"""合并审批卡片的发送（ADR-021）。

CI 绿时发审批卡片，CI 红时发失败信息卡片。两者都走 ``ApprovalCard``，
通过 ``hitl_kind="merge_approval`` 区分标题与按钮渲染。

收件目标用独立的 ``CODE_REVIEW_MERGE_CHANNEL``，不复用审查通知的
``CODE_REVIEW_FEISHU_CHANNEL``：合并审批是"人做决策"的动作，与审查报告
的推送目标分开，避免审查报告把审批卡片淹没。
"""

from __future__ import annotations

import logging
from typing import Any

from app.channels.feishu_cards import ApprovalCard, FeishuCardSender
from app.deps import _card_sender as _global_card_sender
from apps.code_review_pipeline.merge_store import MergeRecord

logger = logging.getLogger("moa.merge_notifier")


class MergeNotifier:
    def __init__(
        self,
        card_sender: FeishuCardSender | None = None,
        default_target: str | None = None,
    ) -> None:
        self._card_sender = card_sender
        self._default_target = default_target

    async def send_approval_card(self, record: MergeRecord) -> bool:
        """CI 绿时发审批卡片。"""
        if not self._default_target:
            logger.info(
                "no merge channel configured (CODE_REVIEW_MERGE_CHANNEL); "
                "skipping approval card for %s",
                record.task_key,
            )
            return False

        card_sender = self._card_sender or _global_card_sender
        if card_sender is None:
            logger.warning("feishu card sender not initialized; skip merge card")
            return False

        card = self._build_approval_card(record)
        card.target = self._default_target
        ok = await card_sender.send_card(card)
        if ok:
            logger.info("merge approval card sent for %s", record.task_key)
        return ok

    async def send_ci_failure_card(self, record: MergeRecord) -> bool:
        """CI 红时发失败信息卡片（列出失败的 job 名与链接）。"""
        if not self._default_target:
            logger.info(
                "no merge channel configured (CODE_REVIEW_MERGE_CHANNEL); "
                "skipping CI failure card for %s",
                record.task_key,
            )
            return False

        card_sender = self._card_sender or _global_card_sender
        if card_sender is None:
            logger.warning("feishu card sender not initialized; skip CI failure card")
            return False

        card = self._build_ci_failure_card(record)
        card.target = self._default_target
        ok = await card_sender.send_card(card)
        if ok:
            logger.info("CI failure card sent for %s", record.task_key)
        return ok

    def _build_approval_card(self, record: MergeRecord) -> ApprovalCard:
        """CI 绿时的审批卡片。"""
        output = (
            f"PR: {record.repo}#{record.pr_number}\n"
            f"SHA: {record.head_sha}\n"
            f"CI: {record.ci_state or 'success'}"
        )
        return ApprovalCard(
            session_id=record.task_key,
            trace_id=record.task_key,
            agent_name="merge_gate",
            intent="merge_approval",
            agent_output=output,
            channel="feishu",
            target=self._default_target or "",
            hitl_kind="merge_approval",
            applicant="",
            reason="CI 通过，等待人工审批合并",
        )

    def _build_ci_failure_card(self, record: MergeRecord) -> ApprovalCard:
        """CI 红时的失败信息卡片。"""
        lines = [
            f"PR: {record.repo}#{record.pr_number}",
            f"SHA: {record.head_sha}",
            f"CI: {record.ci_state or 'failure'}",
        ]
        if record.ci_failing:
            lines.append("")
            lines.append("失败的 check:")
            for check in record.ci_failing:
                name = check.get("name", "unknown")
                url = check.get("url", "")
                if url:
                    lines.append(f"  - {name}: {url}")
                else:
                    lines.append(f"  - {name}")
        return ApprovalCard(
            session_id=record.task_key,
            trace_id=record.task_key,
            agent_name="merge_gate",
            intent="merge_approval",
            agent_output="\n".join(lines),
            channel="feishu",
            target=self._default_target or "",
            hitl_kind="merge_approval",
            applicant="",
            reason="CI 失败，请检查后重试",
        )
