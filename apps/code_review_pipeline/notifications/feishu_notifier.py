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

        card_sender = self._card_sender or _global_card_sender
        if card_sender is None:
            logger.warning("feishu card sender not initialized; skip notification")
            return

        if notification.overall_need_human_review:
            card = self._build_review_card(notification)
        else:
            card = self._build_summary_card(notification)

        target = self._default_target or notification.repo
        card.target = target

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
