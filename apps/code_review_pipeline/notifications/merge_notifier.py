"""合并审批卡片的发送（ADR-021）。

CI 绿时发审批卡片，CI 红时发失败信息卡片。两者都走 ``ApprovalCard``，
通过 ``hitl_kind="merge_approval`` 区分标题与按钮渲染。

收件目标用独立的 ``CODE_REVIEW_MERGE_CHANNEL``，不复用审查通知的
``CODE_REVIEW_FEISHU_CHANNEL``：合并审批是"人做决策"的动作，与审查报告
的推送目标分开，避免审查报告把审批卡片淹没。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.channels.feishu_cards import ApprovalCard, FeishuCardSender
from app.deps import _card_sender as _global_card_sender
from app.deps import engine as _global_engine
from app.engine import HitlRequest
from apps.code_review_pipeline.merge_store import MergeRecord

logger = logging.getLogger("moa.merge_notifier")


class MergeNotifier:
    def __init__(
        self,
        card_sender: FeishuCardSender | None = None,
        default_target: str | None = None,
        session_store: Any | None = None,
    ) -> None:
        self._card_sender = card_sender
        self._default_target = default_target
        # 可注入：默认取 ``app.deps.engine`` 的 SessionStore（生产是 Redis），
        # 单测传假的即可，不必碰真 Redis。
        self._session_store = session_store

    @property
    def _hitl_store(self) -> Any:
        return (
            self._session_store
            if self._session_store is not None
            else _global_engine.session_store
        )

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
        # 先登记待批记录，再发卡：卡片一到用户就可能点，登记晚一步那次点击就落空。
        self._register_pending_approval(record)
        ok = await card_sender.send_card(card)
        if ok:
            logger.info("merge approval card sent for %s", record.task_key)
        else:
            # 卡片没发出去 → 撤掉登记，不留一条永远无人点击的待批记录。
            self._withdraw_pending_approval(record)
            logger.warning(
                "merge approval card not sent; withdrew pending approval for %s",
                record.task_key,
            )
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
        return ApprovalCard(
            session_id=record.task_key,
            trace_id=record.task_key,
            agent_name="merge_gate",
            intent="merge_approval",
            agent_output=self._approval_output(record),
            channel="feishu",
            target=self._default_target or "",
            hitl_kind="merge_approval",
            applicant="",
            reason="CI 通过，等待人工审批合并",
        )

    @staticmethod
    def _approval_output(record: MergeRecord) -> str:
        return (
            f"PR: {record.repo}#{record.pr_number}\n"
            f"SHA: {record.head_sha}\n"
            f"CI: {record.ci_state or 'success'}"
        )

    def _register_pending_approval(self, record: MergeRecord) -> None:
        """把这条合并审批登记进 HITL 待批存储，让卡片按钮点得动。

        **这是审批闭环此前缺的一环**（2026-10-09 实测）：卡片按钮的 value 只带
        ``session_id`` / ``trace_id``，回调侧用 ``hitl_id = trace_id or session_id``
        去 ``pop_hitl`` 认领。发卡却不登记，点击必然落到"该审批请求已失效或已被
        处理"——卡片上的"批准"是一个点了没反应的承诺，合并永远不会执行。

        存储走 ``engine.session_store``（生产是 Redis，见 ``app/deps.py``），所以
        独立进程里的 merge worker 登记后，gateway 收回调时能认领到。
        """
        request = HitlRequest(
            session_id=record.task_key,
            trace_id=record.task_key,
            agent_output=self._approval_output(record),
            intent="merge_approval",
            agent_name="merge_gate",
            channel="feishu",
            target=self._default_target or "",
            created_at=time.time(),
            hitl_kind="merge_approval",
            applicant="",
            reason="CI 通过，等待人工审批合并",
        )
        self._hitl_store.store_hitl(record.task_key, request)

    def _withdraw_pending_approval(self, record: MergeRecord) -> None:
        self._hitl_store.remove_hitl(record.task_key)

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
            # 失败卡片**没有可批准的东西**：CI 是红的，人唯一能做的是去看日志、
            # 转给 agent 修。渲染批准/拒绝按钮等于给一个点了必然无效的承诺
            # （见 feishu_cards.py 的 _NO_ACTION_KINDS 注释）。
            hitl_kind="merge_ci_failure",
            applicant="",
            reason="CI 失败，请检查后重试",
        )
