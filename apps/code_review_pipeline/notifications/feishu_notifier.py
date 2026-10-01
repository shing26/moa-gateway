from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.channels.feishu_cards import ApprovalCard, FeishuCardSender, parse_card_callback
from app.deps import _card_sender as _global_card_sender

logger = logging.getLogger("moa.code_review.notifications")


@dataclass(frozen=True)
class ReviewNotification:
    trace_id: str
    repo: str
    pr_number: int
    author: str
    changed_files: int
    overall_need_human_review: bool = False
    findings_by_severity: dict[str, int] | None = None
    report: Any = None


class FeishuReviewNotifier:
    def __init__(self, webhook_url: str | None = None, card_sender: FeishuCardSender | None = None, default_target: str | None = None) -> None:
        self._webhook_url = webhook_url
        self._card_sender = card_sender
        self._default_target = default_target

    async def send_summary(self, notification: ReviewNotification) -> None:
        logger.info(
            "feishu summary trace=%s repo=%s pr=%d author=%s files=%d need_review=%s severities=%s",
            notification.trace_id,
            notification.repo,
            notification.pr_number,
            notification.author,
            notification.changed_files,
            notification.overall_need_human_review,
            notification.findings_by_severity or {},
        )

        # **没有明确配置的收件目标就不发**。
        #
        # 此前 target 回落成 notification.repo（也就是 "owner/repo" 这个字符串），
        # 而 card_sender 又会借用聊天链路那套飞书凭据。结果是：只要 .env 里配了
        # FEISHU_APP_ID/SECRET（那是给聊天 webhook 用的），PR 审查链路就会拿着一个
        # repo 名当 chat_id 去发卡片——实测在 golden path 里真的换到了 token 并
        # "发送成功"。
        #
        # 这不是"多发了一条通知"，而是**审查链路擅自复用了聊天链路的凭据和目标**。
        # 两件事分开配、各自决定发不发，才是能预期的行为。
        #
        # 判据只看 _default_target，不看 _webhook_url：``FeishuCardSender.send_card``
        # 是拿 ``card.target`` 当 receive_id、并且固定用 receive_id_type=chat_id 去
        # 调 im/v1/messages 的，``_webhook_url`` 在这条发送路径上**根本没被读**。
        # 拿 webhook 存在当"已配置"的凭证，会在真正发出去时仍然把 repo 名当 chat_id。
        if not self._default_target:
            logger.info(
                "no notification destination configured (FEISHU_HOME_CHANNEL); "
                "skipping feishu summary trace=%s",
                notification.trace_id,
            )
            return

        card_sender = self._card_sender or _global_card_sender
        if card_sender is None:
            logger.warning("feishu card sender not initialized; skip notification")
            return

        if notification.overall_need_human_review:
            card = self._build_review_card(notification)
        else:
            card = self._build_summary_card(notification)

        card.target = self._default_target

        ok = await card_sender.send_card(card)
        if not ok:
            logger.warning("feishu notification delivery failed trace=%s", notification.trace_id)

    @staticmethod
    def _build_summary_card(notification: ReviewNotification) -> ApprovalCard:
        severity = (notification.findings_by_severity or {}).get("critical", 0) + (notification.findings_by_severity or {}).get("high", 0)
        recommendation = (notification.report.recommendation if getattr(notification, "report", None) else "comment") or "comment"
        header_template = "green" if recommendation == "approve" else "yellow"
        lines = [
            f"**仓库**: {notification.repo}",
            f"**PR**: #{notification.pr_number}",
            f"**作者**: {notification.author}",
            f"**变更文件**: {notification.changed_files}",
            f"**严重问题**: {severity}",
            f"**结论**: {recommendation}",
        ]
        if notification.report is not None:
            summary = str(getattr(notification.report, "summary", "") or "").strip()
            if summary:
                lines.append(f"**总结**: {summary[:200]}")

        content = "\n".join(lines)
        return ApprovalCard(
            session_id=str(notification.trace_id),
            trace_id=notification.trace_id,
            agent_name="code-review-report",
            intent="review_summary",
            agent_output=content,
            channel="feishu",
            target="",
        )

    @staticmethod
    def _build_review_card(notification: ReviewNotification) -> ApprovalCard:
        """需要人工复核时的**通知**卡片——故意不带批准/拒绝按钮。

        这条链路从不 ``store_hitl``（`apps/` 下 0 处），所以按钮点了必然回
        "该审批已失效"。此前渲染成审批卡片等于承诺一个做不到的动作
        （2026-09-23 修正为 notification 形态；真要做可审批，得先给这条链路接
        HITL 存储与回调，那是独立议题）。
        """
        severity = (notification.findings_by_severity or {}).get("critical", 0) + (notification.findings_by_severity or {}).get("high", 0)
        recommendation = (notification.report.recommendation if getattr(notification, "report", None) else "comment") or "comment"
        content = (
            f"**仓库**: {notification.repo}\n"
            f"**PR**: #{notification.pr_number}\n"
            f"**作者**: {notification.author}\n"
            f"**严重问题**: {severity}\n"
            f"**结论**: {recommendation}\n"
            f"**Trace**: {notification.trace_id}\n"
            "**需要人工复核**（本卡片为通知，不支持在此批准/拒绝）\n"
        )
        if notification.report is not None:
            summary = str(getattr(notification.report, "summary", "") or "").strip()
            if summary:
                content += f"\n{summary[:400]}"
        return ApprovalCard(
            session_id=str(notification.trace_id),
            trace_id=notification.trace_id,
            agent_name="code-review-report",
            intent="human_in_the_loop",
            agent_output=content,
            channel="feishu",
            target="",
            hitl_kind="notification",
        )

    @staticmethod
    def from_env() -> FeishuReviewNotifier:
        import os

        webhook_url = os.getenv("FEISHU_REVIEW_WEBHOOK")
        default_target = os.getenv("FEISHU_HOME_CHANNEL")
        card_sender = None
        from app.config import settings

        app_id = settings.feishu_app_id
        app_secret = settings.feishu_app_secret
        if app_id and app_secret:
            try:
                from app.channels.feishu_auth import FeishuAuthConfig, FeishuTokenProvider
                from app.channels.feishu_cards import FeishuCardSender

                auth_provider = FeishuTokenProvider(FeishuAuthConfig(app_id=app_id, app_secret=app_secret))
                card_sender = FeishuCardSender(auth_provider)
            except Exception as exc:
                logger.warning("init feishu card sender failed: %s", exc)
        return FeishuReviewNotifier(webhook_url=webhook_url, card_sender=card_sender, default_target=default_target)
