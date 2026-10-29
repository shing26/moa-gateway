from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app.channels.feishu_cards import ApprovalCard, FeishuCardSender, parse_card_callback
from app.deps import _card_sender as _global_card_sender

logger = logging.getLogger("moa.code_review.notifications")

_FINDING_PAGE_SIZE = 5
_DISPOSITION_BUTTONS = (
    ("可行动", "accepted_actionable", "", "primary"),
    ("非行动", "accepted_non_actionable", "not_worth_fixing", "default"),
    ("误报", "false_positive", "incorrect_finding", "danger"),
    ("重复", "duplicate", "", "default"),
    ("已知", "already_known", "", "default"),
)


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


@dataclass(frozen=True)
class FindingReviewCard:
    """Interactive review card with per-finding feedback actions."""

    trace_id: str
    repo: str
    pr_number: int
    head_sha: str
    review_run_id: str = ""
    findings: tuple[Any, ...] = ()
    page: int = 1
    total_pages: int = 1
    control: bool = False
    session_id: str = ""
    channel: str = "feishu"
    target: str = ""
    feedback_by_uid: dict[str, str] | None = None

    def to_card_payload(self) -> dict[str, Any]:
        if self.control:
            elements: list[dict[str, Any]] = [
                {
                    "tag": "markdown",
                    "content": (
                        f"**仓库**: {self.repo}\n"
                        f"**PR**: #{self.pr_number}\n"
                        f"**Head**: `{self.head_sha[:12]}`\n"
                        "所有 finding 已完成人工标注。"
                    ),
                },
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "批准写回 GitHub"},
                            "value": {
                                "action": "review_publish",
                                "trace_id": self.trace_id,
                                "head_sha": self.head_sha,
                                "review_run_id": self.review_run_id,
                            },
                            "type": "primary",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "拒绝写回"},
                            "value": {
                                "action": "review_reject",
                                "trace_id": self.trace_id,
                                "head_sha": self.head_sha,
                                "review_run_id": self.review_run_id,
                            },
                            "type": "danger",
                        },
                    ],
                },
            ]
            return {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "Agent Gateway - PR Review 写回审批",
                    },
                    "template": "orange",
                },
                "elements": elements,
            }

        elements = [
            {
                "tag": "markdown",
                "content": (
                    f"**仓库**: {self.repo}\n"
                    f"**PR**: #{self.pr_number}\n"
                    f"**Head**: `{self.head_sha[:12]}`\n"
                    f"**页**: {self.page}/{self.total_pages}"
                ),
            },
            {"tag": "hr"},
        ]
        for index, finding in enumerate(self.findings, start=1):
            evidence = "\n".join(f"- {str(item)[:300]}" for item in finding.evidence[:2])
            current_label = (self.feedback_by_uid or {}).get(finding.finding_uid, "")
            elements.append(
                {
                    "tag": "markdown",
                    "content": (
                        f"**{index}. [{finding.severity.upper()}] {finding.title}**\n"
                        f"`{finding.file}:{finding.line}`\n"
                        f"{finding.description[:500]}\n"
                        f"建议：{finding.suggestion[:400]}\n"
                        + (
                            f"当前标签：`{current_label}`\n"
                            if current_label
                            else "当前标签：未标注\n"
                        )
                        + (f"证据：\n{evidence}" if evidence else "证据：无")
                    ),
                }
            )
            rows = []
            for start, size in ((0, 3), (3, 2)):
                actions = []
                for label, disposition, reason_code, style in _DISPOSITION_BUTTONS[
                    start : start + size
                ]:
                    actions.append(
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": label},
                            "value": {
                                "action": "review_finding_feedback",
                                "trace_id": self.trace_id,
                                "finding_uid": finding.finding_uid,
                                "disposition": disposition,
                                "reason_code": reason_code,
                                "head_sha": self.head_sha,
                                "review_run_id": self.review_run_id,
                                "page": self.page,
                                "total_pages": self.total_pages,
                            },
                            "type": style,
                        }
                    )
                rows.append({"tag": "action", "actions": actions})
            elements.extend(rows)
            elements.append({"tag": "hr"})
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "Agent Gateway - PR Review 逐条反馈",
                },
                "template": "blue",
            },
            "elements": elements,
        }

    def to_message_payload(self) -> dict[str, Any]:
        return {
            "receive_id": self.target,
            "msg_type": "interactive",
            "content": json.dumps(self.to_card_payload(), ensure_ascii=False),
            "uuid": self.delivery_key,
        }

    @property
    def delivery_key(self) -> str:
        kind = "control" if self.control else f"findings:{self.page}:{self.total_pages}"
        material = f"{self.trace_id}\0{self.head_sha}\0{self.review_run_id}\0{kind}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, material))


class FeishuReviewNotifier:
    def __init__(self, webhook_url: str | None = None, card_sender: FeishuCardSender | None = None, default_target: str | None = None) -> None:
        self._webhook_url = webhook_url
        self._card_sender = card_sender
        self._default_target = default_target

    @property
    def is_configured(self) -> bool:
        return bool(self._default_target)

    @staticmethod
    def expected_finding_card_count(findings: tuple[Any, ...]) -> int:
        published = sum(
            str(getattr(item, "status", "")) == "published" for item in findings
        )
        if published == 0:
            return 1
        return (published + _FINDING_PAGE_SIZE - 1) // _FINDING_PAGE_SIZE

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
                "no notification destination configured (CODE_REVIEW_FEISHU_CHANNEL); "
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

    async def send_finding_cards(
        self,
        *,
        trace_id: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        review_run_id: str,
        findings: tuple[Any, ...],
    ) -> int:
        """Send final findings in bounded pages; return number of cards sent."""
        published = tuple(
            item
            for item in findings
            if str(getattr(item, "status", "")) == "published"
        )
        if not self._default_target:
            logger.info(
                "no finding-card destination configured; skipping trace=%s",
                trace_id,
            )
            return 0
        card_sender = self._card_sender or _global_card_sender
        if card_sender is None:
            logger.warning("feishu card sender not initialized; skip finding cards")
            return 0
        if not published:
            cards = (
                FindingReviewCard(
                    trace_id=trace_id,
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    review_run_id=review_run_id,
                    control=True,
                    session_id=trace_id,
                    target=self._default_target,
                ),
            )
        else:
            pages = [
                published[index : index + _FINDING_PAGE_SIZE]
                for index in range(0, len(published), _FINDING_PAGE_SIZE)
            ]
            cards = tuple(
                FindingReviewCard(
                    trace_id=trace_id,
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    review_run_id=review_run_id,
                    findings=tuple(page),
                    page=index + 1,
                    total_pages=len(pages),
                    session_id=trace_id,
                    target=self._default_target,
                )
                for index, page in enumerate(pages)
            )
        sent = 0
        for card in cards:
            if await card_sender.send_card(card):  # type: ignore[arg-type]
                sent += 1
        return sent

    async def send_publish_control(
        self,
        *,
        trace_id: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        review_run_id: str,
    ) -> bool:
        if not self._default_target:
            return False
        card_sender = self._card_sender or _global_card_sender
        if card_sender is None:
            return False
        card = FindingReviewCard(
            trace_id=trace_id,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            review_run_id=review_run_id,
            control=True,
            session_id=trace_id,
            target=self._default_target,
        )
        return await card_sender.send_card(card)  # type: ignore[arg-type]

    async def update_feedback_card(
        self,
        *,
        message_id: str,
        trace_id: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        review_run_id: str,
        findings: tuple[Any, ...],
        feedback_by_uid: dict[str, str],
        page: int,
        total_pages: int,
    ) -> bool:
        card_sender = self._card_sender or _global_card_sender
        if card_sender is None:
            return False
        published = tuple(
            item
            for item in findings
            if str(getattr(item, "status", "")) == "published"
        )
        start = max(0, (page - 1) * _FINDING_PAGE_SIZE)
        page_findings = published[start : start + _FINDING_PAGE_SIZE]
        if not page_findings:
            return False
        card = FindingReviewCard(
            trace_id=trace_id,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            review_run_id=review_run_id,
            findings=page_findings,
            page=page,
            total_pages=total_pages,
            session_id=trace_id,
            feedback_by_uid=feedback_by_uid,
        )
        return await card_sender.update_card(message_id, card)

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
        # **审查链路的收件目标是独立的开关**（2026-10-02）。
        #
        # 此前这里读 FEISHU_HOME_CHANNEL——那是**聊天链路**的默认会话。于是一次
        # golden path /demo review 就会往用户自己的聊天窗口里推一张 PR 审查摘要卡，
        # 而用户对这条链路毫无预期：他在跑可靠性底座的演示，不是在订阅审查通知。
        #
        # 与其"配置了聊天目标就默认推审查通知"，不如让这条链路默认沉默、要发必须
        # 显式开口。这也让 send_summary 里那个"没配目标就不发"的守卫真正有意义
        # （它此前读的就是这个变量，所以守卫形同虚设）。
        default_target = os.getenv("CODE_REVIEW_FEISHU_CHANNEL", "")
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
