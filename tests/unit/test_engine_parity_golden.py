"""Golden parity: the three scenarios the second engine is judged on.

FSM pipeline and LangGraph graph run the *same* input with the *same*
collaborators and must agree field by field. Everything is deterministic — no
LLM, no network — because a gate that only runs when a model is reachable is
not a gate. The tool-loop scenario drives the real ``TaskAgent`` with its
offline ``MockTaskLLM``, so the ReAct loop and tool execution are real and only
the "pick the next action" step is rule-based.
"""

from __future__ import annotations

import pytest

from app.agents import loader  # noqa: F401  registers coder/general/review/task
from app.agents.contract import AGENT_REGISTRY
from app.engine import Engine, SessionStore
from app.evaluator.evaluator import RuleEvaluator
from app.fsm.state_machine import Event as FsmEvent
from app.guard.guard_service import guard_service
from app.long_term_memory import LongTermMemory
from app.models.events import MoAEvent, new_trace_id
from app.orchestration.graph import LangGraphOrchestrator
from app.outbound.adapter import ResponseAdapter
from app.pipeline import MoAPipeline
from app.prompt_registry import PromptEntry, PromptRegistry
from app.vectordb import VectorDBClient
from app.vectordb.retriever import RetrievalResult

# Outputs chosen to drive the three real guard outcomes.
ALLOW_OUTPUT = "这是一段正常的项目说明，没有任何敏感信息。"
REVIEW_OUTPUT = "这个方案报价 1999 元/月，包含三次上门服务。"

COMPARED_FIELDS = (
    "status",
    "intent",
    "state",
    "need_human_review",
    "policy_hits",
    "text",
    "agent_name",
    "guard_action",
    # 错误契约（M1）：两条引擎对同一输入必须给出同一个错误码
    "error_code",
)


def _comparable(result):
    return {field: getattr(result, field) for field in COMPARED_FIELDS}


class FakeRetriever:
    def __init__(self, context: str = "retrieved context") -> None:
        self.context = context

    async def retrieve(self, query, session_id=None, user_id=None):
        return RetrievalResult(
            chunks=[self.context], context=self.context, doc_count=1
        )


class FakeFlagClient:
    async def get(self, name, default=False):
        return False


class FakeMemory:
    def get_history(self, session_id):
        return []

    def add(self, session_id, user_msg, assistant_msg):
        pass

    def clear(self, session_id):
        pass


class FakeCommandMode:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, session_id, mode):
        self.store[session_id] = mode

    def get(self, session_id):
        return self.store.get(session_id)

    def clear(self, session_id):
        self.store.pop(session_id, None)


class FixedRouter:
    def __init__(self, intent: str) -> None:
        self.intent = intent

    async def route(self, text):
        return (self.intent, "regex")


class FixedOutputAgent:
    def __init__(self, output: str) -> None:
        self.output = output
        self.envelopes: list = []

    async def execute(self, envelope):
        self.envelopes.append(envelope)
        return self.output


class FailingAgent:
    """每次都抛异常：用来验证两条引擎的重试耗尽路径逐字段等价。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, envelope):
        self.calls += 1
        raise RuntimeError("golden boom")


def make_registry() -> PromptRegistry:
    registry = PromptRegistry()
    for name in ("coder", "general", "review", "task"):
        registry.register(
            PromptEntry(
                agent_name=name, version="stable", system_prompt=f"you are {name}"
            )
        )
        registry.set_active(name, "stable")
    return registry


def make_event(text: str, session_id: str, user_id: str = "") -> MoAEvent:
    return MoAEvent(
        trace_id=new_trace_id(),
        event=FsmEvent.MESSAGE_RECEIVED,
        session_id=session_id,
        text=text,
        context={"source": "golden"},
        user_id=user_id,
    )


def build_pair(
    monkeypatch,
    *,
    output: str = ALLOW_OUTPUT,
    intent: str = "coding",
    agent_key: str = "coder",
    agent=None,
    long_term_memory=None,
    retriever_context: str = "retrieved context",
):
    """Wire both engines onto the same collaborators, mirroring deps.py."""
    if agent is None:
        agent = FixedOutputAgent(output)
    monkeypatch.setitem(AGENT_REGISTRY, agent_key, agent)

    router = FixedRouter(intent)
    registry = make_registry()
    store = SessionStore()
    adapter = ResponseAdapter()
    evaluator = RuleEvaluator()
    engine = Engine(router=router, adapter=adapter, session_store=store)

    pipeline = MoAPipeline(
        engine=engine,
        router=router,
        memory=FakeMemory(),
        adapter=adapter,
        evaluator=evaluator,
        retriever=FakeRetriever(retriever_context),
        prompt_registry=registry,
        flag_client=FakeFlagClient(),
        guard_service=guard_service,
        command_mode=FakeCommandMode(),
        long_term_memory=long_term_memory,
    )
    graph = LangGraphOrchestrator(
        router=router,
        retriever=FakeRetriever(retriever_context),
        prompt_registry=registry,
        flag_client=FakeFlagClient(),
        guard_service=guard_service,
        evaluator=evaluator,
        adapter=adapter,
        memory=FakeMemory(),
        session_store=store,
        engine=engine,
        long_term_memory=long_term_memory,
    )
    return pipeline, graph, engine, store, agent


@pytest.mark.asyncio
async def test_golden_plain_answer_matches(monkeypatch) -> None:
    pipeline, graph, _engine, _store, _agent = build_pair(monkeypatch)

    fsm_result = await pipeline.run(
        make_event("解释一下这个模块", "plain-fsm"), channel="test", target="t1"
    )
    graph_result = await graph.run(
        make_event("解释一下这个模块", "plain-graph"), channel="test", target="t1"
    )

    assert fsm_result.status == "ok"
    assert graph_result.status == "ok"
    assert _comparable(graph_result) == _comparable(fsm_result)
    assert fsm_result.text == ALLOW_OUTPUT


@pytest.mark.asyncio
async def test_golden_tool_loop_matches(monkeypatch) -> None:
    task_agent = AGENT_REGISTRY["task"]
    pipeline, graph, _engine, _store, _agent = build_pair(
        monkeypatch, intent="task", agent_key="task", agent=task_agent
    )

    fsm_result = await pipeline.run(
        make_event("帮我算 3*7", "tool-fsm"), channel="test", target="t1"
    )
    graph_result = await graph.run(
        make_event("帮我算 3*7", "tool-graph"), channel="test", target="t1"
    )

    assert fsm_result.status == "ok"
    assert "21" in fsm_result.text, "calculator tool must actually run"
    assert _comparable(graph_result) == _comparable(fsm_result)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_golden_review_and_resume_match(monkeypatch, decision) -> None:
    pipeline, graph, _engine, store, _agent = build_pair(
        monkeypatch, output=REVIEW_OUTPUT
    )

    fsm_event = make_event("给我一份报价", "review-fsm")
    graph_event = make_event("给我一份报价", "review-graph")
    fsm_result = await pipeline.run(fsm_event, channel="test", target="t1")
    graph_result = await graph.run(graph_event, channel="test", target="t1")

    assert fsm_result.status == "pending_review"
    assert _comparable(graph_result) == _comparable(fsm_result)

    resumed = await graph.resume(graph_event.trace_id, decision)
    if decision == "approve":
        assert resumed.status == "approved"
        assert resumed.text == REVIEW_OUTPUT
    else:
        assert resumed.status == "rejected"
        assert resumed.text == ""
    assert store.get_hitl(graph_event.trace_id) is None


@pytest.mark.asyncio
async def test_review_leaves_fsm_suspended_so_webhook_can_approve(monkeypatch) -> None:
    """The Feishu callback drives the FSM; HUMAN_APPROVED is only legal from SUSPENDED."""
    _pipeline, graph, engine, _store, _agent = build_pair(
        monkeypatch, output=REVIEW_OUTPUT
    )
    event = make_event("给我一份报价", "hook")

    result = await graph.run(event, channel="feishu", target="chat-1")
    assert result.status == "pending_review"

    ctx = engine.peek("hook")
    assert ctx is not None and ctx.state.value == "SUSPENDED"

    # Exactly what app/routes/webhook.py does — it must not raise.
    approved = await engine.handle_event(
        MoAEvent(
            trace_id=event.trace_id,
            event=FsmEvent.HUMAN_APPROVED,
            session_id="hook",
            text="",
            context={"source": "feishu_card_callback"},
        )
    )
    assert approved.context.state.value == "EXECUTING"


@pytest.mark.asyncio
async def test_second_message_on_same_session_starts_fresh(monkeypatch) -> None:
    """Regression: keying the checkpoint by session id made REVIEW sessions stick."""
    _pipeline, graph, _engine, _store, _agent = build_pair(monkeypatch)
    session = "sticky"

    first = await graph.run(make_event("第一条", session), channel="test", target="t1")
    second = await graph.run(make_event("第二条", session), channel="test", target="t1")

    assert first.status == "ok"
    assert second.status == "ok", "second message must not replay an old thread"

    snapshot = graph._graph.get_state(
        {"configurable": {"thread_id": second.trace_id}}
    )
    assert snapshot.values["node_path"] == [
        "route",
        "retrieve",
        "execute",
        "evaluate",
        "guard",
        "deliver",
    ]


@pytest.mark.asyncio
async def test_long_term_memory_recall_parity(monkeypatch) -> None:
    ltm = LongTermMemory(VectorDBClient())
    await ltm.remember("u1", "name", "小张", label="名字", session_id="earlier")
    agent = FixedOutputAgent(ALLOW_OUTPUT)
    pipeline, graph, _engine, _store, _agent = build_pair(
        monkeypatch, agent=agent, long_term_memory=ltm
    )

    await pipeline.run(
        make_event("我的名字是什么", "mem-fsm", user_id="u1"),
        channel="test",
        target="t1",
    )
    await graph.run(
        make_event("我的名字是什么", "mem-graph", user_id="u1"),
        channel="test",
        target="t1",
    )

    summaries = [envelope.global_summary for envelope in agent.envelopes]
    assert len(summaries) == 2
    assert summaries[0] == summaries[1]
    assert "小张" in summaries[0]


@pytest.mark.asyncio
async def test_long_term_memory_write_parity(monkeypatch) -> None:
    ltm = LongTermMemory(VectorDBClient())
    pipeline, graph, _engine, _store, _agent = build_pair(
        monkeypatch, long_term_memory=ltm
    )

    await pipeline.run(
        make_event("记住我的名字是小张", "w-fsm", user_id="fsm-user"),
        channel="test",
        target="t1",
    )
    await graph.run(
        make_event("记住我的名字是小张", "w-graph", user_id="graph-user"),
        channel="test",
        target="t1",
    )

    for user in ("fsm-user", "graph-user"):
        docs = await ltm.list_for(user)
        assert len(docs) == 1, f"{user} should have exactly one memory slot"
        assert "小张" in docs[0].content


@pytest.mark.asyncio
async def test_golden_failure_escalation_matches(monkeypatch) -> None:
    """重试预算耗尽 → 两条引擎都升级人工，且逐字段一致（ADR-010）。

    失败路径是"看起来等价、其实分叉"最容易发生的地方，所以它也要进 golden。
    """
    pipeline, graph, engine, _store, _agent = build_pair(
        monkeypatch, agent=FailingAgent()
    )

    fsm_result = await pipeline.run(
        make_event("随便问点什么", "fail-fsm"), channel="test", target="t1"
    )
    graph_result = await graph.run(
        make_event("随便问点什么", "fail-graph"), channel="test", target="t1"
    )

    assert fsm_result.status == "pending_review"
    assert graph_result.status == "pending_review"
    assert _comparable(graph_result) == _comparable(fsm_result)

    # 重试真的发生过，且两条引擎报同一个数
    assert fsm_result.retry_count == 1
    assert graph_result.retry_count == 1
    assert fsm_result.hitl_kind == graph_result.hitl_kind == "failure_escalation"
    assert "boom" in fsm_result.retry_reason
    # 会话真身都停在 SUSPENDED（RETRY --TASK_FAILED--> SUSPENDED）
    assert engine.peek("fail-fsm").state.value == "SUSPENDED"
    assert engine.peek("fail-graph").state.value == "SUSPENDED"
