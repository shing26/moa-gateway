"""LangGraph orchestration adapter (optional dependency, not the default runtime).

Why this file exists
--------------------
`MoAPipeline.run()` in ``app/pipeline.py`` sequences the request path in one
imperative method: route → retrieve → prompt-select → execute → evaluate →
guard → HITL → deliver. This module expresses *the same sequence* as a
``StateGraph`` so the two can be compared on identical inputs with identical
collaborators.

What is reused, what is duplicated
----------------------------------
Every piece of business logic is the *same object* the FSM path uses — router,
agents, retriever, prompt registry, evaluator, guard service, HITL store,
outbound adapter, conversation memory. This module owns **sequencing only**.
Two consequences worth stating plainly, because they are the fair questions to
ask:

* The three-level guard precedence is *imported* (``_merge_guard``), not
  re-implemented. One definition, so the two runtimes cannot drift apart.
* The FSM vocabulary is still authoritative: every state this graph reports is
  produced by ``next_state()`` from ``app.fsm.state_machine``. If the graph
  took a transition the FSM forbids, ``InvalidStateTransitionException`` fires
  at runtime. LangGraph replaces the *engine*, not the transition table.

Deliberate scope limits (not oversights)
----------------------------------------
These are pinned by ``tests/unit/test_langgraph_adapter.py``, which introspects
``MoAPipeline.__init__`` and fails if the collaborator surface changes without a
decision — so the list below cannot quietly go stale.

* Only the message path is modelled. Slash-commands (``command_mode``),
  ``reset``/``cancel`` and the ``sensitive_pending`` session short-circuit live
  in ``MoAPipeline`` and are not duplicated here.
* Feishu approval-card delivery (``card_sender``) stays in the route layer.
  This adapter persists the HITL request and exposes the interrupt payload; it
  does not send cards.
* ``long_term_memory`` is modelled the same way the FSM pipeline does it:
  recall context merges into ``retrieve``, apply_ops runs in ``deliver``.
  It used to be unmodelled when first added to ``MoAPipeline``; the drift
  guard above now pins it as a modelled collaborator.
* Persistence is ``InMemorySaver`` by default. Swapping in a Redis/Postgres
  checkpointer is the interesting production question and is left to the
  caller via ``checkpointer=``.

What building this surfaced about the FSM (worth knowing)
--------------------------------------------------------
The transition table has no ``ROUTED -> EXECUTING`` edge: ``EXECUTING`` is only
reachable via ``SUSPENDED + HUMAN_APPROVED``. But ``MoAPipeline`` executes the
agent *before* the guard inspects its output, so its happy path never passes
through ``EXECUTING`` or ``OUTPUT_READY`` at all — those states are reached only
on the approval path (``app/routes/webhook.py``). The reported ``PipelineResult.state``
values therefore match on both runtimes precisely because this adapter does not
invent a transition the table forbids; the ``node_path`` field carries the real
execution trace instead.

The ``interrupt()`` replay rule
-------------------------------
A LangGraph node that calls ``interrupt()`` **re-executes from its first line**
when the graph is resumed, and the state updates of that node are not committed
at interrupt time. So "persist the pending approval" and "wait for the human"
must be two separate nodes. Collapsing them into one would re-write the HITL
record on every resume — harmless for an idempotent key, but it also means the
``pending_review`` status would never survive in the checkpoint, and the caller
would see an empty result on the first invocation.
"""

from __future__ import annotations

import logging
import operator
import os
import time
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import app.agents.loader  # noqa: F401  (import for agent registration side effects)
from app.agents.contract import AgentEnvelope, get_agent
from app.agents.intent_map import resolve_agent_key
from app.engine import HitlRequest
from app.guard.guard_service import GuardianAction, GuardVerdict
from app.guard.rbac import Role
from app.long_term_memory import extract_memory_ops
from app.models.errors import ErrorCode
from app.models.events import MoAEvent
from app.fsm.state_machine import Event as FsmEvent
from app.fsm.state_machine import State as FsmState
from app.fsm.state_machine import next_state
from app.pipeline import PipelineResult
from app.pipeline import _merge_guard  # 复用同一条守卫优先级规则，避免两套运行时漂移
from app.prompt_registry.canary import CanaryConfig, select_canary_version

logger = logging.getLogger("moa.orchestration.langgraph")


class GraphState(TypedDict, total=False):
    """Request state. Mirrors the fields ``PipelineResult`` exposes."""

    trace_id: str
    session_id: str
    user_id: str
    text: str
    channel: str
    target: str
    # FSM state, always derived through next_state() — never assigned blindly.
    fsm_state: str
    intent: str
    agent_name: str
    route_fallback: str
    prompt_version: str
    system_prompt: str
    retrieved_chunks: int
    retrieved_context: str
    raw_output: str
    eval_score: float
    guard_action: str
    guard_reason: str
    policy_hits: tuple[str, ...]
    hitl_id: str
    hitl_decision: str
    status: str
    delivered_text: str
    llm_model: str
    cost_usd: float
    llm_latency_ms: float
    fallback_used: str
    error: str
    # 错误契约（M1）：error 存人类可读细节，error_code 存 ErrorCode 字面量；
    # 两者都经 _to_result 进入 PipelineResult，路由层据此产出结构化响应。
    error_code: str
    # Reducer demo: LangGraph appends instead of overwriting, which is how you
    # get an execution trace for free without bolting on a tracer.
    node_path: Annotated[list[str], operator.add]


class LangGraphOrchestrator:
    """Runs the gateway request path on a LangGraph ``StateGraph``."""

    def __init__(
        self,
        *,
        router: Any,
        retriever: Any,
        prompt_registry: Any,
        flag_client: Any,
        guard_service: Any,
        evaluator: Any,
        adapter: Any,
        memory: Any,
        session_store: Any,
        engine: Any = None,
        long_term_memory: Any = None,
        settings_obj: Any = None,
        budget_guard: Any = None,
        checkpointer: Any = None,
    ) -> None:
        self._router = router
        self._retriever = retriever
        self._prompt_registry = prompt_registry
        self._flag_client = flag_client
        self._guard = guard_service
        self._evaluator = evaluator
        self._adapter = adapter
        self._memory = memory
        self._session_store = session_store
        # The FSM Engine stays the owner of the session's FSM state: the graph
        # advances it (MESSAGE_RECEIVED / NEEDS_HUMAN) instead of keeping a
        # second, invisible notion of "where this session is".
        self._engine = engine
        self._long_term_memory = long_term_memory
        self._settings = settings_obj
        # M6：与 FSM 管道共用同一个预算 guard 实例（由 deps 注入）
        self._budget_guard = budget_guard
        self._checkpointer = checkpointer or InMemorySaver()
        self._graph = self._build()

    # ── construction ───────────────────────────────────────────────────────

    @classmethod
    def from_deps(cls, **kwargs: Any) -> "LangGraphOrchestrator":
        """Wire against the same singletons the FSM pipeline uses."""
        from app import deps

        return cls(
            router=deps.router,
            retriever=deps._retriever,
            prompt_registry=deps._prompt_registry,
            flag_client=deps._flag_client,
            guard_service=deps.guard_service,
            evaluator=deps.evaluator,
            adapter=deps.adapter,
            memory=deps.memory,
            session_store=deps.engine.session_store,
            engine=deps.engine,
            long_term_memory=deps.long_term_memory,
            settings_obj=deps.settings,
            budget_guard=getattr(deps, "budget_guard", None),
            **kwargs,
        )

    def _build(self) -> Any:
        graph = StateGraph(GraphState)
        graph.add_node("route", self._node_route)
        graph.add_node("retrieve", self._node_retrieve)
        graph.add_node("execute", self._node_execute)
        graph.add_node("evaluate", self._node_evaluate)
        graph.add_node("guard", self._node_guard)
        graph.add_node("prepare_hitl", self._node_prepare_hitl)
        graph.add_node("await_human", self._node_await_human)
        graph.add_node("deliver", self._node_deliver)
        graph.add_node("blocked", self._node_blocked)
        graph.add_node("rejected", self._node_rejected)

        graph.add_edge(START, "route")
        graph.add_edge("route", "retrieve")
        graph.add_edge("retrieve", "execute")
        # M6：execute 起点做预算预检，超限短路到 blocked，不再进入评估
        graph.add_conditional_edges(
            "execute",
            self._after_execute,
            {"ok": "evaluate", "budget_blocked": "blocked"},
        )
        graph.add_edge("evaluate", "guard")
        graph.add_conditional_edges(
            "guard",
            self._after_guard,
            {"allow": "deliver", "review": "prepare_hitl", "deny": "blocked"},
        )
        graph.add_edge("prepare_hitl", "await_human")
        graph.add_conditional_edges(
            "await_human",
            self._after_human,
            {"approve": "deliver", "reject": "rejected"},
        )
        graph.add_edge("deliver", END)
        graph.add_edge("blocked", END)
        graph.add_edge("rejected", END)
        return graph.compile(checkpointer=self._checkpointer)

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _after_execute(state: GraphState) -> str:
        """M6：execute 后的分流——预算预检短路时直接去 blocked。"""
        if state.get("error_code") == ErrorCode.BUDGET_EXCEEDED.value:
            return "budget_blocked"
        return "ok"

    @staticmethod
    def _advance(current: FsmState, event: FsmEvent) -> str:
        """Move the FSM state through the project's own transition table.

        Raises ``InvalidStateTransitionException`` if this graph attempts a
        transition the FSM forbids — the transition table stays authoritative.
        """
        return next_state(current, event).value

    def _hitl_enabled(self) -> bool:
        if self._settings is not None:
            return bool(getattr(self._settings, "hitl_enabled", True))
        return bool(os.environ.get("MOA_HITL_ENABLED", "true").lower() in ("1", "true", "yes"))

    # ── nodes ──────────────────────────────────────────────────────────────

    async def _node_route(self, state: GraphState) -> dict[str, Any]:
        text = state["text"]
        intent, fallback = await self._router.route(text)
        mapped_key = resolve_agent_key(intent)
        agent = get_agent(mapped_key) or get_agent("general")
        agent_name = mapped_key if agent else "general"
        return {
            "fsm_state": self._advance(FsmState.INIT, FsmEvent.MESSAGE_RECEIVED),
            "intent": intent,
            "agent_name": agent_name,
            "route_fallback": fallback,
            "node_path": ["route"],
        }

    async def _node_retrieve(self, state: GraphState) -> dict[str, Any]:
        session_id = state["session_id"]
        retrieval = await self._retriever.retrieve(state["text"], session_id=session_id)

        # Same merge MoAPipeline performs, so both engines hand the agent the
        # same context string.
        user_id = state.get("user_id", "")
        memory_ops = (
            extract_memory_ops(state["text"])
            if (self._long_term_memory is not None and user_id)
            else []
        )
        recall_allowed = not any(op.action.startswith("forget") for op in memory_ops)
        memory_context = ""
        if self._long_term_memory is not None and user_id and recall_allowed:
            try:
                memory_context = await self._long_term_memory.recall_context(
                    state["text"], user_id
                )
            except Exception:  # noqa: BLE001 - recall must never break a request
                logger.warning("长期记忆召回失败 user=%s", user_id, exc_info=True)
        global_summary = "\n\n---\n\n".join(
            part for part in (memory_context, retrieval.context) if part
        )

        canary_enabled = await self._flag_client.get("canary.enabled", False)
        canary_pct = await self._flag_client.get("canary.traffic_pct", 10)
        canary = CanaryConfig(enabled=bool(canary_enabled), traffic_pct=int(canary_pct))
        entry, version = select_canary_version(
            session_id, self._prompt_registry, state["agent_name"], canary
        )
        return {
            "retrieved_chunks": retrieval.doc_count,
            "retrieved_context": global_summary,
            "prompt_version": version,
            "system_prompt": entry.system_prompt if entry else "",
            "node_path": ["retrieve"],
        }

    async def _node_execute(self, state: GraphState) -> dict[str, Any]:
        # M6：预算预检（limit<=0 的 guard.check 恒 True，零行为差异）
        if self._budget_guard is not None and not self._budget_guard.check(state["session_id"]):
            logger.warning(
                "session budget exceeded session=%s (langgraph) limit=%s",
                state["session_id"], self._budget_guard.limit_usd,
            )
            return {
                "status": "blocked",
                "guard_reason": "该会话的预算额度已用完，请求被拒绝",
                "guard_action": "budget_exceeded",
                "error_code": ErrorCode.BUDGET_EXCEEDED.value,
                "node_path": ["execute"],
            }

        agent = get_agent(state["agent_name"]) or get_agent("general")
        if agent is None:
            return {
                "error": "no agent registered",
                "error_code": ErrorCode.AGENT_NOT_REGISTERED.value,
                "status": "error",
                "node_path": ["execute"],
            }

        history = self._memory.get_history(state["session_id"])
        envelope = AgentEnvelope(
            trace_id=state["trace_id"],
            session_id=state["session_id"],
            user_raw_input=state["text"],
            global_summary=state.get("retrieved_context", ""),
            history=tuple(history),
            agent_local_slot={
                "intent": state.get("intent", ""),
                "resource": state.get("intent", ""),
                "prompt_version": state.get("prompt_version", ""),
                "system_prompt": state.get("system_prompt", ""),
            },
        )
        try:
            raw_output = await agent.execute(envelope)
        except Exception as exc:  # noqa: BLE001 - mirrored from MoAPipeline
            logger.exception("langgraph orchestrator: agent execution failed")
            return {
                "error": str(exc),
                "error_code": ErrorCode.AGENT_FAILED.value,
                "status": "error",
                "node_path": ["execute"],
            }

        metrics = envelope.agent_local_slot.get("llm_metrics") or {}
        # M6：调用后累计真实成本（超限影响的是该会话的"下一次"请求）
        if self._budget_guard is not None:
            self._budget_guard.record(state["session_id"], float(metrics.get("cost_usd", 0.0) or 0.0))
        return {
            "raw_output": raw_output,
            "llm_model": str(metrics.get("model_used", "")),
            "cost_usd": float(metrics.get("cost_usd", 0.0)),
            "llm_latency_ms": float(metrics.get("llm_latency_ms", 0.0)),
            "fallback_used": str(metrics.get("fallback_used", "")),
            "node_path": ["execute"],
        }

    async def _node_evaluate(self, state: GraphState) -> dict[str, Any]:
        result = await self._evaluator.score(state.get("raw_output", ""), state.get("intent", ""))
        return {"eval_score": float(result.score), "node_path": ["evaluate"]}

    async def _node_guard(self, state: GraphState) -> dict[str, Any]:
        raw_output = state.get("raw_output", "")
        agent_name = state.get("agent_name", "general")
        intent = state.get("intent", "assistant")
        payload = {"intent": intent, "resource": intent, "role": os.environ.get("MOA_DEFAULT_ROLE", "operator")}

        guard_intent = intent
        hitl_enabled = self._hitl_enabled()
        if "EXECUTION_REQUIRES_APPROVAL" in raw_output:
            guard_intent = "execute_code"
            hitl_enabled = True

        verdict = self._guard.evaluate(agent_name, guard_intent, payload, hitl_enabled=hitl_enabled)
        policy_ids: tuple[str, ...] = ()
        if verdict.action != GuardianAction.DENY:
            try:
                role = Role(payload.get("role", "operator"))
            except ValueError:
                role = None
            try:
                output_verdict, policy_ids = self._guard.evaluate_output(
                    raw_output, intent=guard_intent, role=role, hitl_enabled=hitl_enabled
                )
            except Exception:  # noqa: BLE001 - mirrored from MoAPipeline
                output_verdict = GuardVerdict(action=GuardianAction.ALLOW, reason="ok")
                policy_ids = ()
            verdict = _merge_guard(verdict, output_verdict, policy_ids)

        return {
            "guard_action": verdict.action.value,
            "guard_reason": verdict.reason,
            "policy_hits": policy_ids,
            "node_path": ["guard"],
        }

    async def _node_prepare_hitl(self, state: GraphState) -> dict[str, Any]:
        hitl = HitlRequest(
            session_id=state["session_id"],
            trace_id=state["trace_id"],
            agent_output=state.get("raw_output", ""),
            intent=state.get("intent", ""),
            agent_name=state.get("agent_name", "general"),
            channel=state.get("channel", ""),
            target=state.get("target", ""),
            created_at=time.time(),
        )
        hitl_id = state["trace_id"] or state["session_id"]
        self._session_store.store_hitl(state["session_id"], hitl)
        if self._engine is not None:
            # The approval callback (app/routes/webhook.py) drives the FSM:
            # HUMAN_APPROVED is only legal from SUSPENDED, so the graph has to
            # leave the session in that state rather than only in its own
            # checkpoint.
            await self._engine.handle_event(
                MoAEvent(
                    trace_id=state.get("trace_id", ""),
                    event=FsmEvent.NEEDS_HUMAN,
                    session_id=state["session_id"],
                    text=state.get("text", ""),
                    context={"source": "langgraph_hitl"},
                )
            )
        return {
            "hitl_id": hitl_id,
            "status": "pending_review",
            "fsm_state": self._advance(FsmState.ROUTED, FsmEvent.NEEDS_HUMAN),
            "node_path": ["prepare_hitl"],
        }

    async def _node_await_human(self, state: GraphState) -> dict[str, Any]:
        # This node replays from its first line on resume; keep it pure.
        decision = interrupt(
            {
                "kind": "hitl",
                "trace_id": state.get("trace_id", ""),
                "session_id": state.get("session_id", ""),
                "agent_name": state.get("agent_name", ""),
                "intent": state.get("intent", ""),
                "guard_reason": state.get("guard_reason", ""),
                "policy_hits": list(state.get("policy_hits", ())),
                "agent_output": state.get("raw_output", "")[:2000],
            }
        )
        normalized = "approve" if str(decision).lower() in ("approve", "approved", "ok", "true") else "reject"
        fsm_event = FsmEvent.HUMAN_APPROVED if normalized == "approve" else FsmEvent.HUMAN_REJECTED
        return {
            "hitl_decision": normalized,
            "fsm_state": self._advance(FsmState.SUSPENDED, fsm_event),
            "node_path": ["await_human"],
        }

    async def _node_deliver(self, state: GraphState) -> dict[str, Any]:
        raw_output = state.get("raw_output", "")
        if state.get("hitl_decision"):
            # Mirror the Feishu callback path: approval releases the stored output.
            hitl = self._session_store.get_hitl(state.get("hitl_id", ""))
            if hitl is not None:
                raw_output = hitl.agent_output
                self._session_store.remove_hitl(state["hitl_id"])
        response = self._adapter.adapt(raw_output, channel=state.get("channel", ""), target=state.get("target", ""))
        self._memory.add(state["session_id"], state["text"], response.text)

        user_id = state.get("user_id", "")
        if self._long_term_memory is not None and user_id:
            try:
                applied = await self._long_term_memory.apply_ops(
                    user_id,
                    extract_memory_ops(state.get("text", "")),
                    session_id=state.get("session_id"),
                )
                if applied:
                    logger.info("长期记忆更新 user=%s ops=%s", user_id, applied)
            except Exception:  # noqa: BLE001 - write failure must not lose the reply
                logger.warning("长期记忆写入失败 user=%s", user_id, exc_info=True)
        # Deliberately does NOT advance fsm_state. The transition table has no
        # ROUTED -> EXECUTING edge: in the FSM, EXECUTING is only reachable via
        # human approval. MoAPipeline executes the agent *before* the guard runs,
        # so its happy path never passes through EXECUTING/OUTPUT_READY either —
        # inventing that edge here would report a state the FSM forbids.
        return {
            "delivered_text": response.text,
            "status": "approved" if state.get("hitl_decision") else "ok",
            "node_path": ["deliver"],
        }

    async def _node_blocked(self, state: GraphState) -> dict[str, Any]:
        return {
            "delivered_text": state.get("guard_reason", "blocked by guard"),
            "status": "blocked",
            "node_path": ["blocked"],
        }

    async def _node_rejected(self, state: GraphState) -> dict[str, Any]:
        if state.get("hitl_id"):
            self._session_store.remove_hitl(state["hitl_id"])
        return {
            "delivered_text": "",
            "status": "rejected",
            "node_path": ["rejected"],
        }

    # ── routing ────────────────────────────────────────────────────────────

    @staticmethod
    def _after_guard(state: GraphState) -> str:
        action = state.get("guard_action", GuardianAction.ALLOW.value)
        if action == GuardianAction.DENY.value:
            return "deny"
        if action == GuardianAction.REVIEW.value:
            return "review"
        return "allow"

    @staticmethod
    def _after_human(state: GraphState) -> str:
        return "approve" if state.get("hitl_decision") == "approve" else "reject"

    # ── public API ─────────────────────────────────────────────────────────

    def _config(self, thread_id: str) -> dict[str, Any]:
        """Checkpoint key.

        Keyed by ``trace_id``, not ``session_id``: a thread that ended on
        ``interrupt()`` stays resumable, and reusing the session id as the
        thread id made the *next* message on that session replay into the
        unfinished interrupt instead of starting a fresh request. Session
        continuity is owned by ``SessionStore`` + the FSM, not by this
        checkpoint.
        """
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _initial_state(event: MoAEvent, *, channel: str, target: str) -> GraphState:
        return {
            "trace_id": event.trace_id,
            "session_id": event.session_id,
            "user_id": getattr(event, "user_id", "") or "",
            "text": (event.text or "").strip(),
            "channel": channel,
            "target": target,
            "fsm_state": FsmState.INIT.value,
            "node_path": [],
        }

    @classmethod
    def _to_result(cls, state: dict[str, Any]) -> PipelineResult:
        interrupted = "__interrupt__" in state
        status = "pending_review" if interrupted else state.get("status", "ok")
        text = state.get("delivered_text", "")
        if status == "error":
            # 此前 error 字段在此处被丢弃（PipelineResult 无处安放），
            # status="error" 时 text=""——路由层的 500 从此有了细节可用。
            text = state.get("error") or text
        if status == "pending_review":
            # Mirrors MoAPipeline's suspension message so the two runtimes are
            # field-for-field interchangeable; the parity test locks this.
            text = "Output requires human approval before delivery"
        return PipelineResult(
            trace_id=state.get("trace_id", ""),
            state=state.get("fsm_state", FsmState.INIT.value),
            intent=state.get("intent", ""),
            text=text,
            status=status,
            need_human_review=status in ("pending_review", "rejected"),
            fallback=state.get("route_fallback", ""),
            policy_hits=tuple(state.get("policy_hits", ()) or ()),
            llm_model=state.get("llm_model", ""),
            cost_usd=float(state.get("cost_usd", 0.0) or 0.0),
            llm_latency_ms=float(state.get("llm_latency_ms", 0.0) or 0.0),
            fallback_used=state.get("fallback_used", ""),
            agent_name=state.get("agent_name", ""),
            guard_action=state.get("guard_action", ""),
            error_code=state.get("error_code", ""),
        )

    async def run(
        self,
        event: MoAEvent,
        *,
        channel: str,
        target: str,
        request: Any | None = None,
    ) -> PipelineResult:
        """Run one request. Returns the same ``PipelineResult`` the FSM path does.

        ``request`` exists so this signature matches ``MoAPipeline.run``; the
        graph never writes the request log itself — ``EngineDispatcher`` writes
        it once, for both engines.
        """
        if self._engine is not None:
            # Mirror MoAPipeline: advance the FSM first, so a later NEEDS_HUMAN
            # (or a Feishu approval callback) lands on a legal transition.
            await self._engine.handle_event(event)
        state = await self._graph.ainvoke(
            self._initial_state(event, channel=channel, target=target),
            self._config(event.trace_id),
        )
        return self._to_result(state)

    async def resume(self, thread_id: str, decision: str) -> PipelineResult:
        """Resume a suspended run — the ``Command(resume=...)`` half of HITL.

        ``thread_id`` is the ``trace_id`` of the suspended request. Production
        approvals do not come through here: the Feishu callback resolves the
        HITL record from ``SessionStore`` and drives the FSM, which is why this
        method deliberately leaves the FSM state alone (two entry points must
        not both advance the same session).
        """
        state = await self._graph.ainvoke(Command(resume=decision), self._config(thread_id))
        return self._to_result(state)

    def pending_payload(self, thread_id: str) -> dict[str, Any] | None:
        """Inspect the interrupt payload of a suspended run (no side effects)."""
        snapshot = self._graph.get_state(self._config(thread_id))
        interrupts = getattr(snapshot, "interrupts", ()) or ()
        if not interrupts:
            return None
        value = interrupts[0].value
        return dict(value) if isinstance(value, dict) else {"value": value}

    def describe(self) -> dict[str, str]:
        """This object's engine identity, surfaced by ``/healthz``."""
        return {"engine": "langgraph"}


__all__ = ["GraphState", "LangGraphOrchestrator"]
