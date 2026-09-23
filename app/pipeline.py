from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from app.agents.contract import AgentEnvelope, get_agent
from app.agents.intent_map import INTENT_AGENT_MAP, resolve_agent_key
from app.agents.retry import AgentExecutionFailed, execute_with_retry
import app.agents.loader
from app.channels.feishu_cards import ApprovalCard
from app.command_mode import MODES, parse_command
from app.config import settings
from app.context_budget import apply_context_budget
from app.engine import HitlRequest
from app.fsm.state_machine import Event as FsmEvent
from app.guard.guard_service import GuardianAction, GuardVerdict
from app.guard.rbac import Role
from app.models.errors import ErrorCode
from app.long_term_memory import extract_memory_ops
from app.middleware.request_logger import bind_context_stats, bind_trace, log_request
from app.models.events import MoAEvent
from app.prompt_registry.canary import CanaryConfig, select_canary_version

logger = logging.getLogger("moa.pipeline")

# 重试预算耗尽后用户看到的话。两条引擎共用同一个字面量——图路径的 _to_result
# 会按 hitl_kind 分支取它，否则失败升级会被硬编码成"待审批的输出"的文案。
FAILURE_ESCALATION_TEXT = "自动处理失败，已转人工处理"


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
    # ADR-010：重试与审批来源。dispatcher 据此给两条引擎写同一份审计 extra，
    # 图路径因此不需要再补一套字段。
    retry_count: int = 0
    retry_reason: str = ""
    hitl_kind: str = ""
    # 评估结果：dispatcher 用它们给图路径写审计（此前图路径算了 eval_score 却
    # 无处安放，审计里永远是 None）。None 表示"没跑评测"，与 0.0（判为危险）不同
    # ——这个区分是 2026-09-22 接 eval_score 到审计时定下的语义。
    eval_score: float | None = None
    eval_issues: tuple[str, ...] = ()
    # 工具活动：tool_calls=3 / tool_errors=3 表示"三个工具全失败但任务仍返回了结果"。
    # 没有这两个数，"任务完成"与"优雅失败"在审计里长得一样。
    tool_calls: int = 0
    tool_errors: int = 0


def _merge_guard(
    verdict: GuardVerdict,
    output_verdict: GuardVerdict,
    eval_verdict: GuardVerdict | None = None,
) -> GuardVerdict:
    """合并三个判定来源，优先级 DENY > REVIEW。

    第三个参数此前是调用方传进来却从未被使用的 ``policy_ids``（死参数），现在
    承载评估器判定——它是第四道闸门：guard 策略查的是合规（内网 IP / 密钥 /
    价格承诺），评估器查的是**输出本身**的完整性与危险性。评估器判 DENY 时不可
    审批（危险代码不是"要不要批准"的问题），判 REVIEW 时进人工。
    """
    if verdict.action == GuardianAction.DENY:
        return verdict
    if output_verdict.action == GuardianAction.DENY:
        return output_verdict
    if eval_verdict is not None and eval_verdict.action == GuardianAction.DENY:
        return eval_verdict
    if output_verdict.action == GuardianAction.REVIEW:
        return output_verdict
    if eval_verdict is not None and eval_verdict.action == GuardianAction.REVIEW:
        return eval_verdict
    return verdict


# 这些 issue 前缀代表"输出里含可执行的危险动作"，不可审批只能拦。
_EVAL_DENY_PREFIXES = (
    "dangerous_call:",
    "dangerous_method:",
    "dangerous_import:",
    "dangerous_import_from:",
    "write_mode_open:",
)


def _tool_failure_issues(tool_calls: int, tool_errors: int) -> tuple[str, ...]:
    """工具调用**全部失败**时补一条 issue，让它走评估器那条已有的刹车。

    ReAct 把工具异常降级成 observation 让模型自愈是有意设计（见 ADR-010），但
    "所有工具都失败、任务却照样返回了结果"的答案**不该当正常交付**——它长得像成功。
    此前只做到"审计里可见"（`tool_calls`/`tool_errors`），现在接到已有的刹车：
    issue → REVIEW → 人工。**判定是"全部失败"（`tool_calls > 0 and tool_errors == tool_calls`），
    不是"有失败"**——部分失败仍属模型该自己收敛的情形。
    """
    if tool_calls > 0 and tool_errors == tool_calls:
        return ("all_tool_calls_failed",)
    return ()


def _verdict_from_eval_issues(issues: Any) -> GuardVerdict | None:
    """把评估器的 issues 变成一个 verdict 来源；干净输出返回 None。

    这是"评估器要求人工"从哑标志变成真刹车的那一步：此前
    ``EvalResult.need_human_review`` 只被塞进响应体，没有任何动作，于是一个
    ``empty_output``（score 0.0）但没命中策略规则的输出会以 ``status="ok"``
    正常送达用户。两条引擎共用本函数，判定不会分叉。
    """
    issues = tuple(issues or ())
    if not issues:
        return None
    if any(str(issue).startswith(_EVAL_DENY_PREFIXES) for issue in issues):
        return GuardVerdict(action=GuardianAction.DENY, reason="evaluator: unsafe output")
    return GuardVerdict(action=GuardianAction.REVIEW, reason="evaluator: quality check")


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
        context_budget: Any = None,
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
        # 上下文预算：组合根构造、双引擎共用同一实例；None = 不裁剪（旧行为）。
        self.context_budget = context_budget

    def set_card_sender(self, sender: Any) -> None:
        self.card_sender = sender

    @staticmethod
    def _resolve_user_id(event: MoAEvent) -> str:
        raw = getattr(event, "user_id", "") or event.context.get("user_id", "")
        return str(raw or "").strip()

    async def _advance_execute_state(
        self, event: MoAEvent, fsm_event: FsmEvent, *, attempt: int = 0,
    ) -> str:
        """推进一个执行期 FSM 事件，返回推进后的状态名。

        执行期的状态由 pipeline 在 agent 调用前后主动推进。此前完全没有推进，
        所以 EXECUTING / RETRY / OUTPUT_READY / COMPLETED 都是不可达的装饰状态。
        """
        session_state = await self.engine.handle_event(MoAEvent(
            trace_id=event.trace_id, event=fsm_event,
            session_id=event.session_id, text=event.text,
            context={"source": "pipeline_execute", "attempt": attempt},
        ))
        return session_state.context.state.value

    async def _escalate_failure(
        self,
        *,
        event: MoAEvent,
        request: Any | None,
        start: float,
        agent_name: str,
        intent: str,
        channel: str,
        target: str,
        failure: AgentExecutionFailed,
    ) -> PipelineResult:
        """重试预算耗尽 → 升级人工，而不是只回一句 error 就结束。

        复用 guard REVIEW 的同一套闭环（store_hitl → NEEDS_HUMAN → 卡片 →
        回调 approve/reject → 审计），所以"自动处理失败"从此进入有人看、有留痕、
        可回流评测的通道，而不是静默变成一个 500。
        """
        reason = f"{type(failure.last_error).__name__}: {failure.last_error}"
        digest = (
            f"[自动处理失败] 已尝试 {failure.attempts} 次仍未完成。\n"
            f"失败原因：{reason}\n"
            f"trace_id：{event.trace_id}"
        )
        if not settings.hitl_enabled:
            # HITL 关闭时没有人可以升级，保持原有的错误返回语义。
            if request is not None:
                await log_request(
                    request, 500, (time.monotonic() - start) * 1000,
                    event.session_id, agent_name, intent, "error", event.text,
                    "agent execution failed",
                    retry_count=failure.attempts - 1, retry_reason=reason,
                )
            return PipelineResult(
                trace_id=event.trace_id, state="SUSPENDED", intent=intent,
                text="agent execution failed", status="error",
                agent_name=agent_name, error_code=ErrorCode.AGENT_FAILED.value,
                retry_count=failure.attempts - 1, retry_reason=reason,
                hitl_kind="failure_escalation",
            )
        hitl_request = HitlRequest(
            session_id=event.session_id, trace_id=event.trace_id, agent_output=digest,
            intent=intent, agent_name=agent_name, channel=channel, target=target,
            created_at=time.time(), hitl_kind="failure_escalation",
            applicant=self._resolve_user_id(event), reason=reason,
        )
        self.engine.session_store.store_hitl(event.session_id, hitl_request)
        # NEEDS_HUMAN 在 SUSPENDED 上是自环：发它是为了置上 hitl_pending
        # （dispatcher 据此把该会话的后续消息留给 FSM），状态本身已由
        # RETRY --TASK_FAILED--> SUSPENDED 落定。
        await self.engine.handle_event(MoAEvent(
            trace_id=event.trace_id, event=FsmEvent.NEEDS_HUMAN,
            session_id=event.session_id, text=event.text,
            context={"source": "pipeline_failure_escalation"},
        ))
        if self.card_sender:
            card = ApprovalCard(
                session_id=event.session_id, trace_id=event.trace_id, agent_name=agent_name,
                intent=intent, agent_output=digest, channel=channel, target=target,
                hitl_kind="failure_escalation",
                applicant=self._resolve_user_id(event), reason=reason,
            )
            await self.card_sender.send_card(card)
        if request is not None:
            await log_request(
                request, 200, (time.monotonic() - start) * 1000,
                event.session_id, agent_name, intent, "review", event.text, digest,
                retry_count=failure.attempts - 1, retry_reason=reason,
                hitl_kind="failure_escalation",
            )
        return PipelineResult(
            trace_id=event.trace_id, state="SUSPENDED", intent=intent,
            text=FAILURE_ESCALATION_TEXT, status="pending_review",
            need_human_review=True, agent_name=agent_name,
            guard_action=GuardianAction.REVIEW.value,
            retry_count=failure.attempts - 1, retry_reason=reason,
            hitl_kind="failure_escalation",
        )

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
        # 审计 trace 贯通：本请求内所有审计条目共享 event.trace_id
        bind_trace(event.trace_id)

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

        # 上下文预算（上下文工程）：双引擎共用 apply_context_budget，保证送进
        # agent 的历史与摘要完全一致；决策统计绑定到审计上下文，可按 trace 查询。
        budgeted = apply_context_budget(conversation_history, global_summary, self.context_budget)
        conversation_history, global_summary = budgeted.history, budgeted.summary
        bind_context_stats(budgeted.stats)
        logger.info("context budget: %s", budgeted.stats)

        envelope = AgentEnvelope(
            trace_id=event.trace_id,
            session_id=event.session_id,
            user_raw_input=event.text,
            global_summary=global_summary,
            history=tuple(conversation_history),
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

        # 执行期状态推进：TASK_STARTED 把 FSM 从 ROUTED 推到 EXECUTING；每次失败由
        # on_failure 落 RETRY，第二次失败时状态机表自己把它推到 SUSPENDED（表即重试
        # 预算），成功后由 TASK_SUCCESS 落 OUTPUT_READY。见 ADR-010。
        state = await self._advance_execute_state(event, FsmEvent.TASK_STARTED)

        async def _on_failure(attempt: int, exc: BaseException) -> None:
            await self._advance_execute_state(
                event, FsmEvent.TASK_FAILED, attempt=attempt,
            )

        try:
            raw_output, attempts, _ = await execute_with_retry(
                agent, envelope, on_failure=_on_failure,
            )
        except asyncio.CancelledError:
            raise
        except AgentExecutionFailed as failure:
            return await self._escalate_failure(
                event=event, request=request, start=start,
                agent_name=agent_name, intent=intent,
                channel=channel, target=target, failure=failure,
            )
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

        state = await self._advance_execute_state(event, FsmEvent.TASK_SUCCESS)
        # 审计记的是"重试了几次"（不含首次尝试），所以没重试时为 0、字段缺席。
        retry_count = attempts - 1

        llm_metrics = envelope.agent_local_slot.get("llm_metrics") or {}
        llm_model = str(llm_metrics.get("model_used", ""))
        cost_usd = float(llm_metrics.get("cost_usd", 0.0))
        llm_latency_ms = float(llm_metrics.get("llm_latency_ms", 0.0))
        fallback_used = str(llm_metrics.get("fallback_used", ""))
        # 工具活动：只有真调工具且真的报告了的 agent（当前是 TaskAgent）会写这两个键。
        # tool_calls=3/tool_errors=3 就是"三个工具全失败却仍返回了结果"。
        tool_calls = int(envelope.agent_local_slot.get("tool_calls_total", 0) or 0)
        tool_errors = int(envelope.agent_local_slot.get("tool_errors_total", 0) or 0)
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
        # 审批来源：只有评估器判定真的赢下合并时才标记，guard 触发时留空以保持
        # 既有审计语义（collect_hitl_feedback 按 guard_action 配对决策）。
        hitl_kind = ""
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
            eval_verdict = _verdict_from_eval_issues(
                eval_result.issues + _tool_failure_issues(tool_calls, tool_errors)
            )
            merged = _merge_guard(verdict, output_verdict, eval_verdict)
            if eval_verdict is not None and merged is eval_verdict:
                hitl_kind = (
                    "eval_deny"
                    if eval_verdict.action == GuardianAction.DENY
                    else "eval_review"
                )
            verdict = merged

        if verdict.action == GuardianAction.REVIEW:
            hitl_request = HitlRequest(
                session_id=event.session_id, trace_id=event.trace_id, agent_output=raw_output,
                intent=intent, agent_name=agent_name, channel=channel, target=target,
                created_at=time.time(), hitl_kind=hitl_kind or "review",
                applicant=user_id, reason=verdict.reason,
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
                    hitl_kind=hitl_kind or "review",
                    applicant=user_id, reason=verdict.reason,
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
                    eval_score=eval_result.score,
                    eval_issues=eval_result.issues,
                    retry_count=retry_count,
                    hitl_kind=hitl_kind,
                    tool_calls=tool_calls,
                    tool_errors=tool_errors,
                    route_fallback=fallback,
                )
            return PipelineResult(
                trace_id=event.trace_id, state="SUSPENDED", intent=intent,
                text="Output requires human approval before delivery",
                status="pending_review", need_human_review=True, policy_hits=policy_ids,
                llm_model=llm_model, cost_usd=cost_usd,
                llm_latency_ms=llm_latency_ms, fallback_used=fallback_used,
                agent_name=agent_name, guard_action=verdict.action.value,
                eval_score=eval_result.score, eval_issues=eval_result.issues,
                hitl_kind=hitl_kind or "review",
                tool_calls=tool_calls, tool_errors=tool_errors,
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
                    eval_score=eval_result.score,
                    eval_issues=eval_result.issues,
                    retry_count=retry_count,
                    hitl_kind=hitl_kind,
                    tool_calls=tool_calls,
                    tool_errors=tool_errors,
                    route_fallback=fallback,
                )
            return PipelineResult(
                trace_id=event.trace_id, state=state, intent=intent,
                text=verdict.reason, status="blocked", policy_hits=policy_ids,
                llm_model=llm_model, cost_usd=cost_usd,
                llm_latency_ms=llm_latency_ms, fallback_used=fallback_used,
                agent_name=agent_name, guard_action=verdict.action.value,
                eval_score=eval_result.score, eval_issues=eval_result.issues,
                hitl_kind=hitl_kind,
                tool_calls=tool_calls, tool_errors=tool_errors,
            )

        response = self.adapter.adapt(raw_output, channel=channel, target=target)
        self.memory.add(event.session_id, event.text, response.text)
        # 交付完成 → COMPLETED（此前该状态没有任何入边，是彻底的死状态）。
        state = await self._advance_execute_state(event, FsmEvent.DELIVERED)
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
                eval_score=eval_result.score,
                eval_issues=eval_result.issues,
                retry_count=retry_count,
                tool_calls=tool_calls,
                tool_errors=tool_errors,
                route_fallback=fallback,
            )
        return PipelineResult(
            trace_id=event.trace_id, state=state, intent=intent,
            text=response.text, status="ok",
            need_human_review=eval_result.need_human_review or verdict.action != GuardianAction.ALLOW,
            fallback=fallback, policy_hits=policy_ids,
            llm_model=llm_model, cost_usd=cost_usd,
            llm_latency_ms=llm_latency_ms, fallback_used=fallback_used,
            agent_name=agent_name, guard_action=verdict.action.value,
            retry_count=retry_count,
            eval_score=eval_result.score, eval_issues=eval_result.issues,
            tool_calls=tool_calls, tool_errors=tool_errors,
        )
