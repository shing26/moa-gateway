from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from app.agents.contract import AgentEnvelope, get_agent
from app.agents.intent_map import INTENT_AGENT_MAP, resolve_agent_key
import app.agents.loader
from app.channels.feishu_cards import ApprovalCard
from app.command_mode import MODES, parse_command
from app.config import settings
from app.context_budget import compact_history, estimate_tokens, fit_text
from app.engine import HitlRequest
from app.fsm.state_machine import Event as FsmEvent
from app.guard.guard_service import GuardianAction, GuardVerdict
from app.guard.rbac import Role
from app.models.errors import ErrorCode
from app.long_term_memory import extract_memory_ops
from app.middleware.request_logger import log_request
from app.models.events import MoAEvent
from app.prompt_registry.canary import CanaryConfig, select_canary_version

logger = logging.getLogger("moa.pipeline")


@dataclass(frozen=True)
class PipelineResult:
    trace_id: str
    state: str
    intent: str
    text: str
    status: str
    need_human_review: bool = False
    fallback: str = ""
    policy_hits: tuple[str, ...] = ()
    llm_model: str = ""
    cost_usd: float = 0.0
    llm_latency_ms: float = 0.0
    fallback_used: str = ""
    # 供跨引擎的统一审计使用：图路径也算得出同样的两个值，dispatcher 因此
    # 不必为两条引擎各写一份 log_request。
    agent_name: str = ""
    guard_action: str = ""
    # 错误契约（M1）：status="error" 时必填 ErrorCode 字面量，路由层据此
    # 产出结构化响应；FSM 与 LangGraph 两条引擎都必须填（parity 测试钉住）。
    error_code: str = ""


def _merge_guard(verdict: GuardVerdict, output_verdict: GuardVerdict, policy_ids: tuple[str, ...]) -> GuardVerdict:
    if verdict.action == GuardianAction.DENY:
        return verdict
    if output_verdict.action == GuardianAction.DENY:
        return output_verdict
    if output_verdict.action == GuardianAction.REVIEW:
        return output_verdict
    return verdict


class MoAPipeline:
    def __init__(
        self,
        engine: Any,
        router: Any,
        memory: Any,
        adapter: Any,
        evaluator: Any,
        retriever: Any,
        prompt_registry: Any,
        flag_client: Any,
        guard_service: Any,
        command_mode: Any,
        card_sender: Any = None,
        long_term_memory: Any = None,
        budget_guard: Any = None,
    ) -> None:
        self.engine = engine
        self.router = router
        self.memory = memory
        self.adapter = adapter
        self.evaluator = evaluator
        self.retriever = retriever
        self.prompt_registry = prompt_registry
        self.flag_client = flag_client
        self.guard_service = guard_service
        self.command_mode = command_mode
        self.card_sender = card_sender
        # 默认 None：未配置长期记忆时，行为与补齐前完全一致。
        self.long_term_memory = long_term_memory
        # M6：per-session 预算 guard；None 或 limit<=0 时零行为差异。
        self.budget_guard = budget_guard

    def set_card_sender(self, sender: Any) -> None:
        self.card_sender = sender

    @staticmethod
    def _resolve_user_id(event: MoAEvent) -> str:
        raw = getattr(event, "user_id", "") or event.context.get("user_id", "")
        return str(raw or "").strip()

    def describe(self) -> dict[str, str]:
        """This object's engine identity, surfaced by ``/healthz``."""
        return {"engine": "fsm"}

    async def run(
        self,
        event: MoAEvent,
        *,
        channel: str,
        target: str,
        request: Any | None = None,
    ) -> PipelineResult:
        start = time.monotonic()

        session_state = await self.engine.handle_event(event)
        state = session_state.context.state.value

        text = event.text.strip()
        if event.event in (FsmEvent.RESET, FsmEvent.CANCEL):
            self.engine.reset_session(event.session_id)
            self.memory.clear(event.session_id)
            self.command_mode.clear(event.session_id)
            if request is not None:
                await log_request(
                    request, 200, (time.monotonic() - start) * 1000,
                    event.session_id, "control", "control", "reset", event.text, "会话已重置",
                )
            return PipelineResult(
                trace_id=event.trace_id, state="INIT", intent="control",
                text="会话已重置", status="reset",
            )
        if event.event == FsmEvent.SENSITIVE_DETECTED:
            if request is not None:
                await log_request(
                    request, 200, (time.monotonic() - start) * 1000,
                    event.session_id, "guard", "suspended", "suspended", event.text,
                    "检测到敏感内容，消息已挂起",
                )
            return PipelineResult(
                trace_id=event.trace_id, state="SUSPENDED", intent="suspended",
                text="检测到敏感内容，消息已挂起", status="suspended",
            )
        if text.startswith("/"):
            parsed = parse_command(text)
            if parsed:
                cmd_key, label = parsed
                if cmd_key in ("help", ""):
                    help_text = "可用指令:\n/coding - 编程模式\n/translate - 翻译模式\n/search - 搜索模式\n/analyze - 分析模式\n/review - PR 审查\n/default - 默认模式"
                    if request is not None:
                        await log_request(
                            request, 200, (time.monotonic() - start) * 1000,
                            event.session_id, "command", "help", "", event.text, help_text,
                        )
                    return PipelineResult(
                        trace_id=event.trace_id, state="ROUTED", intent="help",
                        text=help_text, status="command",
                    )
                cmd_info = MODES.get(cmd_key, {})
                self.command_mode.set(event.session_id, cmd_info.get("intent") or "")
                mode_label = cmd_info.get("label", cmd_key)
                reply = "已切换至 " + mode_label + " 模式"
                if request is not None:
                    await log_request(
                        request, 200, (time.monotonic() - start) * 1000,
                        event.session_id, "command", cmd_key, "", event.text, reply,
                    )
                return PipelineResult(
                    trace_id=event.trace_id, state="ROUTED", intent=cmd_key,
                    text=reply, status="command",
                )
            reply = "未知指令，发送 /help 查看可用指令"
            if request is not None:
                await log_request(
                    request, 200, (time.monotonic() - start) * 1000,
                    event.session_id, "command", "help", "", event.text, reply,
                )
            return PipelineResult(
                trace_id=event.trace_id, state="ROUTED", intent="help",
                text=reply, status="command",
            )

        session_metadata = getattr(session_state.context, "metadata", {}) or {}
        if session_metadata.get("sensitive_pending"):
            if request is not None:
                await log_request(
                    request, 200, (time.monotonic() - start) * 1000,
                    event.session_id, "guard", "suspended", "suspended", event.text,
                    "会话处于挂起状态，请先处理审批或发送 reset",
                )
            return PipelineResult(
                trace_id=event.trace_id, state="SUSPENDED", intent="suspended",
                text="会话处于挂起状态，请先处理审批或发送 reset", status="suspended",
            )

        intent, fallback = await self.router.route(event.text)
        forced = self.command_mode.get(event.session_id)
        if forced:
            intent = forced
        # 修复根因 A：意图标签（coding/...）经同构映射表对齐到注册键（coder/...），
        # 使 CoderAgent / ReviewAgent 能被正确选中，而非全部塌缩到 GeneralAgent。
        mapped_key = resolve_agent_key(intent)
        agent = get_agent(mapped_key) or get_agent("general")
        agent_name = mapped_key if agent else "general"
        for name in ("coder", "general", "review"):
            if get_agent(name) is agent:
                agent_name = name
                break

        user_id = self._resolve_user_id(event)
        # 显式遗忘请求不应先把自己要删的记忆召回并塞进上下文。
        memory_ops = (
            extract_memory_ops(event.text) if (self.long_term_memory and user_id) else []
        )
        recall_allowed = not any(op.action.startswith("forget") for op in memory_ops)

        retrieval = await self.retriever.retrieve(event.text, session_id=event.session_id)

        memory_context = ""
        if self.long_term_memory is not None and user_id and recall_allowed:
            try:
                memory_context = await self.long_term_memory.recall_context(event.text, user_id)
            except Exception:
                logger.warning("长期记忆召回失败 user=%s", user_id, exc_info=True)
        global_summary = "\n\n---\n\n".join(
            part for part in (memory_context, retrieval.context) if part
        )

        canary_enabled = await self.flag_client.get("canary.enabled", False)
        canary_pct = await self.flag_client.get("canary.traffic_pct", 10)
        canary_config = CanaryConfig(enabled=bool(canary_enabled), traffic_pct=int(canary_pct))
        selected_prompt, selected_version = select_canary_version(
            event.session_id, self.prompt_registry, agent_name, canary_config,
        )

        conversation_history = self.memory.get_history(event.session_id)

        # 上下文预算（上下文工程）：历史按预算从最新往回保留，被裁掉的旧对话压成
        # 一条省略摘要并入 global_summary，整体再按 summary 预算截断——被裁的旧
        # 对话不至于整段失忆，长会话也不会把模型上下文挤爆。
        budget = compact_history(conversation_history, settings.context_history_budget)
        if budget.elision:
            global_summary = (
                f"{global_summary}\n\n---\n\n{budget.elision}" if global_summary else budget.elision
            )
        global_summary, summary_truncated = fit_text(
            global_summary, settings.context_summary_budget
        )
        logger.info(
            "context budget: history %d→%d dropped=%d elided=%s summary_tokens≈%d truncated=%s",
            len(conversation_history), budget.kept_messages, budget.dropped_messages,
            budget.elided, estimate_tokens(global_summary), summary_truncated,
        )

        envelope = AgentEnvelope(
            trace_id=event.trace_id,
            session_id=event.session_id,
            user_raw_input=event.text,
            global_summary=global_summary,
            history=tuple(budget.history),
            agent_local_slot={
                "intent": intent,
                "resource": intent,
                "prompt_version": selected_version,
                "system_prompt": selected_prompt.system_prompt if selected_prompt else "",
            },
        )

        budget_blocked = (
            self.budget_guard is not None
            and not self.budget_guard.check(event.session_id)
        )
        if budget_blocked:
            logger.warning(
                "session budget exceeded session=%s limit=%s",
                event.session_id, self.budget_guard.limit_usd,
            )
            if request is not None:
                await log_request(
                    request, 429, (time.monotonic() - start) * 1000,
                    event.session_id, agent_name, intent, "budget_exceeded",
                    event.text, "session budget exceeded",
                )
            return PipelineResult(
                trace_id=event.trace_id, state=state, intent=intent,
                text="该会话的预算额度已用完，请求被拒绝", status="blocked",
                agent_name=agent_name,
                error_code=ErrorCode.BUDGET_EXCEEDED.value,
            )

        try:
            raw_output = await agent.execute(envelope)
        except Exception:
            if request is not None:
                await log_request(
                    request, 500, (time.monotonic() - start) * 1000,
                    event.session_id, agent_name, intent, "error", event.text,
                    "agent execution failed",
                )
            return PipelineResult(
                trace_id=event.trace_id, state=state, intent=intent,
                text="agent execution failed", status="error",
                agent_name=agent_name,
                error_code=ErrorCode.AGENT_FAILED.value,
            )

        llm_metrics = envelope.agent_local_slot.get("llm_metrics") or {}
        llm_model = str(llm_metrics.get("model_used", ""))
        cost_usd = float(llm_metrics.get("cost_usd", 0.0))
        llm_latency_ms = float(llm_metrics.get("llm_latency_ms", 0.0))
        fallback_used = str(llm_metrics.get("fallback_used", ""))
        # M6：调用后累计真实成本（超限影响的是该会话的"下一次"请求）
        if self.budget_guard is not None and cost_usd > 0:
            self.budget_guard.record(event.session_id, cost_usd)

        eval_result = await self.evaluator.score(raw_output, intent)

        payload = {"intent": intent, "resource": intent, "role": os.environ.get("MOA_DEFAULT_ROLE", "operator")}
        guard_intent = intent
        guard_hitl = settings.hitl_enabled
        if "EXECUTION_REQUIRES_APPROVAL" in raw_output:
            guard_intent = "execute_code"
            guard_hitl = True
        verdict = self.guard_service.evaluate(agent_name, guard_intent, payload, hitl_enabled=guard_hitl)
        if verdict.action == GuardianAction.DENY:
            policy_ids: tuple[str, ...] = ()
        else:
            try:
                role = Role(payload.get("role", "operator"))
            except ValueError:
                role = None
            try:
                output_verdict, policy_ids = self.guard_service.evaluate_output(
                    raw_output, intent=guard_intent, role=role, hitl_enabled=guard_hitl,
                )
            except Exception:
                output_verdict = GuardVerdict(action=GuardianAction.ALLOW, reason="ok")
                policy_ids = ()
            verdict = _merge_guard(verdict, output_verdict, policy_ids)

        if verdict.action == GuardianAction.REVIEW:
            hitl_request = HitlRequest(
                session_id=event.session_id, trace_id=event.trace_id, agent_output=raw_output,
                intent=intent, agent_name=agent_name, channel=channel, target=target,
                created_at=time.time(),
            )
            self.engine.session_store.store_hitl(event.session_id, hitl_request)
            await self.engine.handle_event(MoAEvent(
                trace_id=event.trace_id, event=FsmEvent.NEEDS_HUMAN,
                session_id=event.session_id, text=event.text,
                context={"source": "pipeline_guard"},
            ))
            if self.card_sender:
                card = ApprovalCard(
                    session_id=event.session_id, trace_id=event.trace_id, agent_name=agent_name,
                    intent=intent, agent_output=raw_output, channel=channel, target=target,
                )
                await self.card_sender.send_card(card)
            if request is not None:
                await log_request(
                    request, 200, (time.monotonic() - start) * 1000,
                    event.session_id, agent_name, intent, "review", event.text, raw_output,
                    policy_hits=policy_ids,
                    llm_model=llm_model,
                    cost_usd=cost_usd,
                    llm_latency_ms=llm_latency_ms,
                    fallback_used=fallback_used,
                )
            return PipelineResult(
                trace_id=event.trace_id, state="SUSPENDED", intent=intent,
                text="Output requires human approval before delivery",
                status="pending_review", need_human_review=True, policy_hits=policy_ids,
                llm_model=llm_model, cost_usd=cost_usd,
                llm_latency_ms=llm_latency_ms, fallback_used=fallback_used,
                agent_name=agent_name, guard_action=verdict.action.value,
            )

        if verdict.action == GuardianAction.DENY:
            if request is not None:
                await log_request(
                    request, 200, (time.monotonic() - start) * 1000,
                    event.session_id, agent_name, intent, "deny", event.text, verdict.reason,
                    policy_hits=policy_ids,
                    llm_model=llm_model,
                    cost_usd=cost_usd,
                    llm_latency_ms=llm_latency_ms,
                    fallback_used=fallback_used,
                )
            return PipelineResult(
                trace_id=event.trace_id, state=state, intent=intent,
                text=verdict.reason, status="blocked", policy_hits=policy_ids,
                llm_model=llm_model, cost_usd=cost_usd,
                llm_latency_ms=llm_latency_ms, fallback_used=fallback_used,
                agent_name=agent_name, guard_action=verdict.action.value,
            )

        response = self.adapter.adapt(raw_output, channel=channel, target=target)
        self.memory.add(event.session_id, event.text, response.text)
        # 记忆写入放在成功路径之后：被 guard 拦截或需要审批的输出不入长期记忆。
        if self.long_term_memory is not None and user_id:
            try:
                applied = await self.long_term_memory.apply_ops(
                    user_id, memory_ops, session_id=event.session_id
                )
                if applied:
                    logger.info("长期记忆更新 user=%s ops=%s", user_id, applied)
            except Exception:
                logger.warning("长期记忆写入失败 user=%s", user_id, exc_info=True)
        if request is not None:
            await log_request(
                request, 200, (time.monotonic() - start) * 1000,
                event.session_id, agent_name, intent, verdict.action.value, event.text, response.text,
                policy_hits=policy_ids,
                llm_model=llm_model,
                cost_usd=cost_usd,
                llm_latency_ms=llm_latency_ms,
                fallback_used=fallback_used,
            )
        return PipelineResult(
            trace_id=event.trace_id, state=state, intent=intent,
            text=response.text, status="ok",
            need_human_review=eval_result.need_human_review or verdict.action != GuardianAction.ALLOW,
            fallback=fallback, policy_hits=policy_ids,
            llm_model=llm_model, cost_usd=cost_usd,
            llm_latency_ms=llm_latency_ms, fallback_used=fallback_used,
            agent_name=agent_name, guard_action=verdict.action.value,
        )
