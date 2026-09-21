"""Parity tests for the optional LangGraph orchestration adapter.

The whole value of ``app/orchestration/graph.py`` rests on one claim: it changes
the *engine*, not the behaviour. These tests hold that claim by running the FSM
pipeline and the LangGraph graph over the same inputs with the same
collaborators and comparing the resulting ``PipelineResult`` field by field.

Skipped entirely when the optional ``langgraph`` extra is not installed, which
is the default install state.
"""

from __future__ import annotations

import pytest

langgraph = pytest.importorskip("langgraph", reason="optional extra: uv sync --extra langgraph")

from app.agents.contract import AGENT_REGISTRY
from app.engine import Engine, SessionStore
from app.evaluator.evaluator import RuleEvaluator
from app.fsm.state_machine import Event as FsmEvent
from app.fsm.state_machine import InvalidStateTransitionException
from app.fsm.state_machine import State as FsmState
from app.guard.guard_service import guard_service
from app.models.events import MoAEvent, new_trace_id
from app.orchestration.graph import LangGraphOrchestrator
from app.outbound.adapter import ResponseAdapter
from app.pipeline import MoAPipeline
from app.prompt_registry import PromptEntry, PromptRegistry
from app.vectordb.retriever import RetrievalResult

# Outputs chosen to exercise the three guard outcomes through real policies.
ALLOW_OUTPUT = "这是一段正常的项目说明，没有任何敏感信息。"
REVIEW_OUTPUT = "这个方案报价 1999 元/月，包含三次上门服务。"
DENY_OUTPUT = "内网数据库地址是 192.168.1.10，端口 5432。"


class FakeRetriever:
    async def retrieve(self, query, session_id=None, user_id=None):
        return RetrievalResult(chunks=["chunk-a"], context="retrieved context", doc_count=1)


class FakeFlagClient:
    async def get(self, name, default=False):
        return False


class FakeRouter:
    async def route(self, text):
        return ("coding", "regex")


class FakeCommandMode:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, session_id, mode):
        self.store[session_id] = mode

    def get(self, session_id):
        return self.store.get(session_id)

    def clear(self, session_id):
        self.store.pop(session_id, None)


class FixedOutputAgent:
    def __init__(self, output: str) -> None:
        self.output = output
        self.envelopes: list = []

    async def execute(self, envelope):
        self.envelopes.append(envelope)
        return self.output


def make_registry() -> PromptRegistry:
    registry = PromptRegistry()
    for name in ("coder", "general", "review", "task"):
        registry.register(
            PromptEntry(agent_name=name, version="stable", system_prompt=f"you are {name}")
        )
        registry.set_active(name, "stable")
    return registry


def make_event(text="解释一下这个模块", session_id="parity-1") -> MoAEvent:
    return MoAEvent(
        trace_id=new_trace_id(),
        event=FsmEvent.MESSAGE_RECEIVED,
        session_id=session_id,
        text=text,
        context={"source": "test"},
    )


def build_both(monkeypatch, *, output: str, session_id: str):
    """Share every collaborator between the two runtimes.

    ``monkeypatch.setitem`` on the registry is what makes agent stubbing apply
    to both: they resolve agents through the same ``AGENT_REGISTRY`` dict.
    """
    agent = FixedOutputAgent(output)
    monkeypatch.setitem(AGENT_REGISTRY, "coder", agent)

    registry = make_registry()
    router = FakeRouter()
    session_store = SessionStore()
    adapter = ResponseAdapter()
    evaluator = RuleEvaluator()
    memory = _FakeMemory()

    pipeline = MoAPipeline(
        engine=Engine(router=router, adapter=adapter, session_store=session_store),
        router=router,
        memory=memory,
        adapter=adapter,
        evaluator=evaluator,
        retriever=FakeRetriever(),
        prompt_registry=registry,
        flag_client=FakeFlagClient(),
        guard_service=guard_service,
        command_mode=FakeCommandMode(),
    )

    orchestrator = LangGraphOrchestrator(
        router=router,
        retriever=FakeRetriever(),
        prompt_registry=registry,
        flag_client=FakeFlagClient(),
        guard_service=guard_service,
        evaluator=evaluator,
        adapter=adapter,
        memory=_FakeMemory(),
        session_store=session_store,
    )
    return pipeline, orchestrator, session_store, agent


class _FakeMemory:
    def __init__(self) -> None:
        self.added: list = []

    def get_history(self, session_id):
        return []

    def add(self, session_id, user_msg, assistant_msg):
        self.added.append((session_id, user_msg, assistant_msg))

    def clear(self, session_id):
        pass


COMPARED_FIELDS = ("status", "intent", "state", "need_human_review", "policy_hits", "text")
# ``fallback`` is compared separately: see test_known_divergence_route_fallback.
#
# ``state`` is compared and must match. That matters more than it looks: the FSM
# table has no ROUTED -> EXECUTING edge, so MoAPipeline's happy path never
# reaches EXECUTING/OUTPUT_READY. The adapter reports the same states rather
# than inventing the missing edge — see the module docstring of
# app/orchestration/graph.py.


def _comparable(result):
    return {field: getattr(result, field) for field in COMPARED_FIELDS}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "expected_status", "expected_state"),
    [
        (ALLOW_OUTPUT, "ok", "ROUTED"),
        (REVIEW_OUTPUT, "pending_review", "SUSPENDED"),
        (DENY_OUTPUT, "blocked", "ROUTED"),
    ],
)
async def test_langgraph_matches_fsm_pipeline(
    monkeypatch, output, expected_status, expected_state
) -> None:
    pipeline, orchestrator, _store, _agent = build_both(
        monkeypatch, output=output, session_id="parity"
    )

    fsm_result = await pipeline.run(make_event(session_id="parity"), channel="test", target="t1")
    graph_result = await orchestrator.run(make_event(session_id="parity"), channel="test", target="t1")

    assert fsm_result.status == expected_status
    assert graph_result.status == expected_status
    assert fsm_result.state == expected_state
    assert graph_result.state == expected_state
    assert _comparable(graph_result) == _comparable(fsm_result)


@pytest.mark.asyncio
async def test_allow_path_policy_hits_empty_and_delivered(monkeypatch) -> None:
    _pipeline, orchestrator, _store, _agent = build_both(
        monkeypatch, output=ALLOW_OUTPUT, session_id="allow"
    )
    result = await orchestrator.run(make_event(session_id="allow"), channel="test", target="t1")
    assert result.status == "ok"
    assert result.policy_hits == ()
    assert result.text == ALLOW_OUTPUT


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [REVIEW_OUTPUT, DENY_OUTPUT])
async def test_known_divergence_route_fallback(monkeypatch, output) -> None:
    """The one field where the two runtimes differ — and it is the FSM side that drops data.

    ``MoAPipeline`` only passes ``fallback=`` on its happy-path return; the
    ``pending_review`` and ``blocked`` returns omit it, so they always report
    ``fallback=""`` even though the router did report a level. This adapter
    reports the router's actual value on every path.

    Pinned here so the divergence stays visible and deliberate: either fix the
    pipeline (and delete this test) or accept it, but do not let it drift
    silently.
    """
    pipeline, orchestrator, _store, _agent = build_both(
        monkeypatch, output=output, session_id="fb"
    )
    fsm_result = await pipeline.run(make_event(session_id="fb"), channel="test", target="t1")
    graph_result = await orchestrator.run(make_event(session_id="fb"), channel="test", target="t1")

    assert fsm_result.fallback == "", "pipeline omits fallback on review/deny branches"
    assert graph_result.fallback == "regex", "adapter reports the router's real fallback level"


@pytest.mark.asyncio
async def test_behavioural_parity_is_not_accidental_same_collaborators(monkeypatch) -> None:
    """Both runtimes must hand the SAME retrieved context to the agent."""
    pipeline, orchestrator, _store, agent = build_both(
        monkeypatch, output=ALLOW_OUTPUT, session_id="ctx"
    )
    await pipeline.run(make_event(session_id="ctx"), channel="test", target="t1")
    await orchestrator.run(make_event(session_id="ctx"), channel="test", target="t1")
    assert [env.global_summary for env in agent.envelopes] == ["retrieved context"] * 2


@pytest.mark.asyncio
async def test_review_path_persists_hitl_and_exposes_interrupt_payload(monkeypatch) -> None:
    _pipeline, orchestrator, store, _agent = build_both(
        monkeypatch, output=REVIEW_OUTPUT, session_id="hitl"
    )
    event = make_event(session_id="hitl")
    result = await orchestrator.run(event, channel="feishu", target="chat-1")

    assert result.status == "pending_review"
    assert result.need_human_review is True
    assert result.policy_hits, "price policy should report at least one policy id"

    stored = store.get_hitl(event.trace_id)
    assert stored is not None
    assert stored.agent_output == REVIEW_OUTPUT
    assert stored.channel == "feishu"

    # 中断线程按 trace_id 取键：会话可能同时有多条待审请求。
    payload = orchestrator.pending_payload(event.trace_id)
    assert payload is not None
    assert payload["kind"] == "hitl"
    assert payload["trace_id"] == event.trace_id
    assert payload["guard_reason"].startswith("policy review")


@pytest.mark.asyncio
async def test_resume_approve_delivers_stored_output(monkeypatch) -> None:
    _pipeline, orchestrator, store, _agent = build_both(
        monkeypatch, output=REVIEW_OUTPUT, session_id="resume-ok"
    )
    event = make_event(session_id="resume-ok")
    await orchestrator.run(event, channel="feishu", target="chat-1")

    resumed = await orchestrator.resume(event.trace_id, "approve")

    assert resumed.status == "approved"
    assert resumed.text == REVIEW_OUTPUT
    assert resumed.state == FsmState.EXECUTING.value
    assert store.get_hitl(event.trace_id) is None, "resolved HITL must be cleared"


@pytest.mark.asyncio
async def test_resume_reject_discards_output(monkeypatch) -> None:
    _pipeline, orchestrator, store, _agent = build_both(
        monkeypatch, output=REVIEW_OUTPUT, session_id="resume-no"
    )
    event = make_event(session_id="resume-no")
    await orchestrator.run(event, channel="feishu", target="chat-1")

    resumed = await orchestrator.resume(event.trace_id, "reject")

    assert resumed.status == "rejected"
    assert resumed.text == ""
    assert resumed.state == FsmState.REJECTED.value
    assert store.get_hitl(event.trace_id) is None


@pytest.mark.asyncio
async def test_node_path_reducer_traces_execution(monkeypatch) -> None:
    _pipeline, orchestrator, _store, _agent = build_both(
        monkeypatch, output=ALLOW_OUTPUT, session_id="trace"
    )
    event = make_event(session_id="trace")
    await orchestrator.run(event, channel="test", target="t1")
    snapshot = orchestrator._graph.get_state(
        {"configurable": {"thread_id": event.trace_id}}
    )
    assert snapshot.values["node_path"] == [
        "route",
        "retrieve",
        "execute",
        "evaluate",
        "guard",
        "deliver",
    ]


def test_graph_state_still_obeys_fsm_transition_table() -> None:
    """LangGraph replaces the engine, not the transition table."""
    assert LangGraphOrchestrator._advance(FsmState.INIT, FsmEvent.MESSAGE_RECEIVED) == "ROUTED"
    assert LangGraphOrchestrator._advance(FsmState.ROUTED, FsmEvent.NEEDS_HUMAN) == "SUSPENDED"
    assert LangGraphOrchestrator._advance(FsmState.SUSPENDED, FsmEvent.HUMAN_APPROVED) == "EXECUTING"
    assert LangGraphOrchestrator._advance(FsmState.SUSPENDED, FsmEvent.HUMAN_REJECTED) == "REJECTED"

    with pytest.raises(InvalidStateTransitionException):
        LangGraphOrchestrator._advance(FsmState.COMPLETED, FsmEvent.NEEDS_HUMAN)


def test_guard_rule_is_imported_not_reimplemented() -> None:
    """Same precedence function as the FSM pipeline — drift is impossible."""
    import app.orchestration.graph as graph_module
    from app.pipeline import _merge_guard

    assert graph_module._merge_guard is _merge_guard


# ── drift guard ────────────────────────────────────────────────────────────
#
# The adapter models a subset of MoAPipeline. That subset was correct when it
# was written; the risk is that the pipeline grows a stage and the adapter
# silently keeps passing its tests because the new stage defaults to off.
#
# That already happened once: ``long_term_memory`` was added to the pipeline
# while this adapter was being built, and every parity test still passed because
# the tests construct the pipeline without it. Hence this guard: adding or
# removing a collaborator forces an explicit decision about which set it
# belongs to.

# Collaborators the adapter reproduces, or consumes through an equivalent route.
# ``engine`` is consumed as ``engine.session_store`` — the graph replaces the
# engine itself, which is the whole point of the exercise.
MODELLED_COLLABORATORS = {
    "engine",
    "router",
    "memory",
    "adapter",
    "evaluator",
    "retriever",
    "prompt_registry",
    "flag_client",
    "guard_service",
    # recall in ``retrieve`` + apply_ops in ``deliver`` — see the parity test
    # for long-term memory in test_engine_parity_golden.py.
    "long_term_memory",
    # M6：execute 起点预检 + 执行后累计，与 FSM 管道共用同一 guard 实例
    "budget_guard",
    # 上下文工程：两引擎共用组合根构造的同一 ContextBudget 实例
    "context_budget",
}

# Deliberately not modelled. See the module docstring of app/orchestration/graph.py.
DECLARED_OUT_OF_SCOPE = {
    "command_mode",  # slash-commands: no LangGraph equivalent worth building
    "card_sender",  # Feishu approval-card delivery stays in the route layer
}

# Of the un-modelled collaborators, these are the ones whose *absence* has to be
# expressed as a default: they perform real per-request work that the adapter
# does not reproduce. ``command_mode`` is different — it is a required
# collaborator that only acts on slash-command input, which no parity test
# sends. Keeping it in this set would assert something untrue about the API.
DEFAULT_OFF_STAGES = {
    "card_sender",
}


def test_pipeline_collaborator_surface_is_fully_accounted_for() -> None:
    import inspect

    from app.pipeline import MoAPipeline

    actual = set(inspect.signature(MoAPipeline.__init__).parameters) - {"self"}
    accounted = MODELLED_COLLABORATORS | DECLARED_OUT_OF_SCOPE

    assert actual == accounted, (
        "MoAPipeline's collaborator surface changed.\n"
        f"  unaccounted for: {sorted(actual - accounted)}\n"
        f"  no longer present: {sorted(accounted - actual)}\n"
        "Decide for each: model it in app/orchestration/graph.py, or add it to "
        "DECLARED_OUT_OF_SCOPE with a reason. Do not let the adapter drift."
    )


def test_out_of_scope_stages_are_actually_off_by_default() -> None:
    """What makes the parity tests valid: the un-modelled stages default to off.

    If a future change makes ``long_term_memory`` mandatory (no default), the
    parity tests would silently start exercising a pipeline the adapter cannot
    match. This test fails first and points at the reason.
    """
    import inspect

    from app.pipeline import MoAPipeline

    params = inspect.signature(MoAPipeline.__init__).parameters
    for name in sorted(DEFAULT_OFF_STAGES):
        assert params[name].default is not inspect.Parameter.empty, (
            f"{name} lost its default — the adapter's parity claims no longer hold"
        )


def test_default_off_stages_are_a_subset_of_unmodelled_collaborators() -> None:
    assert DEFAULT_OFF_STAGES <= DECLARED_OUT_OF_SCOPE
