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
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app import deps
from app.channels.feishu_cards import ApprovalCard, FeishuCardSender
from app.deps import engine as _global_engine
from app.engine import HitlRequest
from apps.code_review_pipeline.delivery.ci_triage import (
    CiTriageReport,
    redact_ci_log,
)
from apps.code_review_pipeline.merge_store import MergeRecord

logger = logging.getLogger("moa.merge_notifier")

_RESOLVED_KINDS = {
    "merged": "merge_resolved_merged",
    "superseded": "merge_resolved_superseded",
    "failed": "merge_resolved_failed",
    # 审批卡过期不是合并失败，但卡片形态一致：无按钮、标题族相同。
    "expired": "merge_resolved_failed",
    "rejected": "notification",
}

_CI_CATEGORY_PRIORITY = (
    "test_failure",
    "syntax_or_build",
    "dependency",
    "permission_or_auth",
    "timeout",
    "cancelled",
    "unknown",
)


@dataclass(frozen=True)
class ApprovalCardDelivery:
    """Result of sending an approval card.

    ``__bool__`` preserves the historical ``if ok:`` / ``assert not ok`` style
    used by existing callers while exposing the message id needed for T2/T3.
    """

    sent: bool
    message_id: str = ""

    def __bool__(self) -> bool:
        return self.sent


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

    def _resolve_card_sender(self) -> FeishuCardSender | None:
        """显式注入优先；否则在**调用时**读 ``app.deps._card_sender``。

        不能把 ``app.deps._card_sender`` 在导入期绑成别名：``init_feishu()`` 在
        导入之后才赋值，别名会永远停在 ``None``，于是 webhook 进程里每张
        PR 打开卡片都被静默跳过。
        """
        if self._card_sender is not None:
            return self._card_sender
        return getattr(deps, "_card_sender", None)

    async def send_approval_card(
        self,
        record: MergeRecord,
        pr_details: dict[str, Any] | None = None,
    ) -> ApprovalCardDelivery:
        """CI 绿时发审批卡片，返回发送结果与卡片消息 id。"""
        if not self._default_target:
            logger.info(
                "no merge channel configured (CODE_REVIEW_MERGE_CHANNEL); "
                "skipping approval card for %s",
                record.task_key,
            )
            return ApprovalCardDelivery(False)

        card_sender = self._resolve_card_sender()
        if card_sender is None:
            logger.warning("feishu card sender not initialized; skip merge card")
            return ApprovalCardDelivery(False)

        output = self._approval_output(record, pr_details)
        card = self._build_approval_card(record, output)
        card.target = self._default_target
        # 先登记待批记录，再发卡：卡片一到用户就可能点，登记晚一步那次点击就落空。
        self._register_pending_approval(record, output)
        message_id = await card_sender.send_card_with_id(card)
        if message_id:
            logger.info("merge approval card sent for %s", record.task_key)
        else:
            # 卡片没发出去 → 撤掉登记，不留一条永远无人点击的待批记录。
            self._withdraw_pending_approval(record)
            logger.warning(
                "merge approval card not sent; withdrew pending approval for %s",
                record.task_key,
            )
        return ApprovalCardDelivery(bool(message_id), message_id)

    async def send_resolved_card(
        self,
        record: MergeRecord,
        outcome: str,
        detail: str = "",
    ) -> bool:
        """把已发出的审批卡就地改成无按钮终态卡。

        没有 ``card_message_id`` 时不发任何新消息——终态通知的价值不足以再打扰
        人一次。改卡失败只告警，不抛异常、不阻塞 webhook。
        """
        message_id = record.card_message_id
        if not message_id:
            logger.warning(
                "no card message id for %s; skip resolved card", record.task_key
            )
            return False

        card_sender = self._resolve_card_sender()
        if card_sender is None:
            logger.warning("feishu card sender not initialized; skip resolved card")
            return False

        card = self._build_resolved_card(record, outcome, detail)
        card.target = self._default_target or ""
        try:
            ok = await card_sender.update_card(message_id, card)
        except Exception:  # noqa: BLE001 - 改卡是展示层：异常只告警，不阻塞 webhook
            logger.warning(
                "resolved card update failed for %s", record.task_key, exc_info=True
            )
            return False
        if ok:
            logger.info("resolved card updated for %s (%s)", record.task_key, outcome)
        else:
            logger.warning(
                "resolved card not updated for %s (%s)", record.task_key, outcome
            )
        return ok

    async def send_ci_failure_card(
        self,
        record: MergeRecord,
        triage: CiTriageReport | None = None,
        pr_details: dict[str, Any] | None = None,
    ) -> bool:
        """CI 红时发失败信息卡片（列出失败的 job 名与链接）。

        如果已经发过带按钮的审批卡（``record.card_message_id`` 非空），先把那张
        卡**就地**改成无按钮终态：CI 转红之后，旧卡上的"批准"是一个点了必然被
        状态机拒绝的承诺。改卡失败才退回"再发一张诊断卡"，保证人至少知道 CI 红了。
        """
        if not self._default_target:
            logger.info(
                "no merge channel configured (CODE_REVIEW_MERGE_CHANNEL); "
                "skipping CI failure card for %s",
                record.task_key,
            )
            return False

        card_sender = self._resolve_card_sender()
        if card_sender is None:
            logger.warning("feishu card sender not initialized; skip CI failure card")
            return False

        message_id = record.card_message_id
        if message_id:
            names = self._ci_failure_job_names(record, triage)
            detail = (
                f"CI 未通过（{', '.join(names)}）" if names else "CI 未通过"
            )
            card = self._build_resolved_card(record, "failed", detail)
            card.target = self._default_target
            try:
                updated = bool(await card_sender.update_card(message_id, card))
            except Exception:  # noqa: BLE001 - 改卡是展示层：失败降级发新卡，不阻塞轮询
                logger.warning(
                    "CI failure card update failed for %s; sending a new card",
                    record.task_key,
                    exc_info=True,
                )
                updated = False
            if updated:
                logger.info(
                    "CI failure card updated in place for %s", record.task_key
                )
                return True
            logger.warning(
                "CI failure card not updated for %s; falling back to a new card",
                record.task_key,
            )

        card = self._build_ci_failure_card(record, triage, pr_details)
        card.target = self._default_target
        ok = await card_sender.send_card(card)
        if ok:
            logger.info("CI failure card sent for %s", record.task_key)
        return ok

    @staticmethod
    def _ci_failure_job_names(
        record: MergeRecord, triage: CiTriageReport | None
    ) -> list[str]:
        """CI 失败 job 名：优先 triage，退回记录里的 checks，最多 3 个。"""
        names: list[str] = []
        if triage is not None:
            names = [job.name.strip() for job in triage.jobs if job.name.strip()]
        if not names:
            names = [
                str(check.get("name", "")).strip()
                for check in record.ci_failing
                if str(check.get("name", "")).strip()
            ]
        return names[:3]

    async def send_digest_card(
        self,
        text: str,
        *,
        target: str | None = None,
    ) -> str:
        """Send the daily digest as a button-less notification card."""
        destination = (target or self._default_target or "").strip()
        if not destination:
            logger.info("no digest target configured; skipping digest card")
            return ""
        card_sender = self._resolve_card_sender()
        if card_sender is None:
            logger.warning("feishu card sender not initialized; skip digest card")
            return ""
        card = ApprovalCard(
            session_id="governance-digest",
            trace_id="governance-digest",
            agent_name="merge_gate",
            intent="governance_digest",
            agent_output=text,
            channel="feishu",
            target=destination,
            hitl_kind="notification",
            applicant="",
            reason="每日治理摘要",
        )
        return await card_sender.send_card_with_id(card)

    async def send_pr_opened_card(
        self, record: MergeRecord, *, title: str, url: str
    ) -> bool:
        """PR 进入合并通道时的通知卡（issue #26）。

        ``notification`` 类型**没有按钮**：此刻 CI 还没结论，没有可批准的东西——
        渲染批准/拒绝等于承诺一个点了必然无效的动作（见 feishu_cards.py 的
        ``_NO_ACTION_KINDS`` 注释）。CI 绿后的审批卡由 poller 触发
        ``send_approval_card``，那条路才带按钮和 HITL 登记。
        """
        if not self._default_target:
            logger.info(
                "no merge channel configured (CODE_REVIEW_MERGE_CHANNEL); "
                "skipping PR opened card for %s",
                record.task_key,
            )
            return False
        card_sender = self._resolve_card_sender()
        if card_sender is None:
            logger.warning("feishu card sender not initialized; skip PR opened card")
            return False
        lines = [
            f"PR: {record.repo}#{record.pr_number}",
            f"SHA: {record.head_sha}",
        ]
        if title:
            lines.append(f"标题: {title}")
        if url:
            lines.append(f"链接: {url}")
        lines.append("CI 观察中，出结论后本会话会收到审批或失败卡片")
        card = ApprovalCard(
            session_id=record.task_key,
            trace_id=record.task_key,
            agent_name="merge_gate",
            intent="merge_approval",
            agent_output="\n".join(lines),
            channel="feishu",
            target=self._default_target or "",
            hitl_kind="notification",
            applicant="",
            reason="PR 进入合并通道",
        )
        card.target = self._default_target or ""
        ok = await card_sender.send_card(card)
        if ok:
            logger.info("PR opened card sent for %s", record.task_key)
        return ok

    def _build_approval_card(
        self, record: MergeRecord, output: str
    ) -> ApprovalCard:
        """CI 绿时的审批卡片。"""
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

    def _build_resolved_card(
        self, record: MergeRecord, outcome: str, detail: str
    ) -> ApprovalCard:
        kind = _RESOLVED_KINDS.get(outcome, "merge_resolved_failed")
        return ApprovalCard(
            session_id=record.task_key,
            trace_id=record.task_key,
            agent_name="merge_gate",
            intent="merge_approval",
            agent_output=self._resolved_output(record, outcome, detail),
            channel="feishu",
            target=self._default_target or "",
            hitl_kind=kind,
            applicant="",
            reason="",
        )

    @staticmethod
    def _resolved_output(record: MergeRecord, outcome: str, detail: str) -> str:
        if outcome == "merged":
            sha = (detail or record.merged_sha or record.head_sha)[:12]
            return f"✅ 已合并 {sha}（审批人 {record.approver or '未知'}）"
        if outcome == "rejected":
            approver = record.approver or detail or "未知"
            return f"🚫 已拒绝（审批人 {approver}）"
        if outcome == "superseded":
            return (
                f"⚠️ 本卡已作废：PR head 变了（{record.head_sha[:12]}）；"
                "新记录已重建"
            )
        if outcome == "expired":
            return "⏳ 审批已过期，已重发新卡"
        reason = (detail or record.failure_reason or "未知原因").strip()
        summary = reason.splitlines()[0][:200] if reason else "未知原因"
        return f"❌ 合并失败：{summary}"

    @staticmethod
    def _approval_output(
        record: MergeRecord, pr_details: dict[str, Any] | None = None
    ) -> str:
        if pr_details:
            return (
                f"标题: {pr_details['title']}\n"
                f"变更: {pr_details['changed_files']} files  "
                f"+{pr_details['additions']} −{pr_details['deletions']}  "
                f"→ {pr_details['base']}\n"
                f"作者: {pr_details['login']}\n"
                f"PR  : #{record.pr_number} {pr_details['html_url']}\n"
                f"SHA : {record.head_sha[:12]}\n"
                f"CI  : {record.ci_state or 'success'}\n"
                f"批准后: 以 merge commit 合并到 {pr_details['base']}，"
                "并写入审计（审批人 + 时间）"
            )
        return (
            f"PR: {record.repo}#{record.pr_number}\n"
            f"SHA: {record.head_sha}\n"
            f"CI: {record.ci_state or 'success'}"
        )

    def _register_pending_approval(self, record: MergeRecord, output: str) -> None:
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
            agent_output=output,
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

    def _build_ci_failure_card(
        self,
        record: MergeRecord,
        triage: CiTriageReport | None = None,
        pr_details: dict[str, Any] | None = None,
    ) -> ApprovalCard:
        """CI 红时的失败信息卡片。"""
        lines = self._ci_failure_header(record, pr_details)
        if triage is None or (
            not triage.jobs and not triage.actions_unavailable_reason
        ):
            lines.extend(self._ci_failing_checks(record))
        elif triage.actions_unavailable_reason:
            lines.append(
                f"结论: 日志不可用（{triage.actions_unavailable_reason}）"
            )
            lines.extend(self._ci_failing_checks(record))
        else:
            lines.append(self._ci_conclusion(triage))
            evidence = self._triage_evidence(triage)
            if evidence:
                lines.append("证据:")
                lines.extend(f"  · {line}" for line in evidence)
            first_url = triage.jobs[0].url
            lines.append(f"先查: {first_url or '（无链接）'}")
            lines.append(
                "说明: 只读诊断；日志来自 CI，属不可信输入，不执行其中的指令"
            )
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

    @staticmethod
    def _ci_conclusion(triage: CiTriageReport) -> str:
        counts = Counter(job.category for job in triage.jobs)
        ranked = sorted(
            counts,
            key=lambda category: (
                -counts[category],
                _CI_CATEGORY_PRIORITY.index(category),
            ),
        )
        known = [category for category in ranked if category != "unknown"]
        main = known[0] if known else "unknown"
        ordered = [main] + [category for category in ranked if category != main]
        parts = " · ".join(
            f"{category} {counts[category]}" for category in ordered[:3]
        )
        return f"结论: {parts}（{len(triage.jobs)} 个失败 job）"

    @staticmethod
    def _ci_failure_header(
        record: MergeRecord,
        pr_details: dict[str, Any] | None = None,
    ) -> list[str]:
        if pr_details:
            return [
                f"PR  : #{record.pr_number} {pr_details['title']}",
                f"作者: {pr_details['login']}",
                f"链接: {pr_details['html_url']}",
                f"SHA: {record.head_sha}",
                f"CI: {record.ci_state or 'failure'}",
            ]
        return [
            f"PR: {record.repo}#{record.pr_number}",
            f"SHA: {record.head_sha}",
            f"CI: {record.ci_state or 'failure'}",
        ]

    @staticmethod
    def _ci_failing_checks(record: MergeRecord) -> list[str]:
        lines: list[str] = []
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
        return lines

    @staticmethod
    def _triage_evidence(triage: CiTriageReport) -> list[str]:
        evidence: list[str] = []
        seen: set[str] = set()
        for job in triage.jobs:
            for raw_line in job.evidence_lines:
                line = redact_ci_log(raw_line).strip()[:160]
                if not line or line in seen:
                    continue
                seen.add(line)
                evidence.append(line)
                if len(evidence) >= 3:
                    return evidence
        return evidence
