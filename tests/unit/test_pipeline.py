from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.pipeline as pipeline_module
from app.engine import Engine
from app.fsm.state_machine import Event as FsmEvent
from app.guard.guard_service import GuardService
from app.guard.rbac import GuardianAction, GuardVerdict
from app.models.events import MoAEvent, new_trace_id
from app.outbound.adapter import ResponseAdapter
from app.pipeline import MoAPipeline
from app.vectordb.retriever import RetrievalResult


class FakeRetriever:
    async def retrieve(self, query, session_id=None, user_id=None):
        return RetrievalResult(chunks=[], context="retrieved context", doc_count=0)


class FakeFlagClient:
    async def get(self, name, default=False):
        return False


class FakeEvaluator:
    def __init__(self, need_human_review=False):
        self.need_human_review = need_human_review

    async def score(self, output_text, intent):
        return SimpleNamespace(score=1.0, need_human_review=self.need_human_review)


class FakeMemory:
    def __init__(self):
        self.added = []

    def get_history(self, session_id):
        return []

    def add(self, session_id, user_msg, assistant_msg):
        self.added.append((session_id, user_msg, assistant_msg))


class FakeRouter:
    def __init__(self, intent="coding", fallback="regex"):
        self.intent = intent
        self.fallback = fallback

    async def route(self, text):
        return (self.intent, self.fallback)


class FakeCommandMode:
    def __init__(self):
        self.store = {}

    def set(self, session_id, mode):
        self.store[session_id] = mode

    def get(self, session_id):
        return self.store.get(session_id)


class FakeGuard:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    def evaluate(self, agent_name, intent, payload, *, hitl_enabled=True):
        self.calls.append((agent_name, intent, payload, hitl_enabled))
        return self.verdict


class OkAgent:
    async def execute(self, envelope):
        return "agent reply"


class RecordingAgent:
    def __init__(self):
        self.envelopes = []

    async def execute(self, envelope):
        self.envelopes.append(envelope)
        return "agent reply"


class RaisingAgent:
    async def execute(self, envelope):
        raise RuntimeError("boom")


class FakeCardSender:
    def __init__(self):
        self.cards = []

    async def send_card(self, card):
        self.cards.append(card)


def make_pipeline(
    engine=None,
    router=None,
    memory=None,
    guard=None,
    command_mode=None,
    card_sender=None,
    agent=None,
):
    return MoAPipeline(
        engine=engine or Engine(),
        router=router or FakeRouter(),
        memory=memory or FakeMemory(),
        adapter=ResponseAdapter(),
        evaluator=FakeEvaluator(),
        retriever=FakeRetriever(),
        prompt_registry=object(),
        flag_client=FakeFlagClient(),
        guard_service=guard or FakeGuard(GuardVerdict(action=GuardianAction.ALLOW, reason="ok")),
        command_mode=command_mode or FakeCommandMode(),
        card_sender=card_sender,
    )


def make_event(text="hello", session_id="s1"):
    return MoAEvent(
        trace_id=new_trace_id(), event=FsmEvent.MESSAGE_RECEIVED,
        session_id=session_id, text=text,
        context={"source": "test"},
    )


def patch_agents(monkeypatch, agent):
    monkeypatch.setattr(
        pipeline_module, "select_canary_version",
        lambda *a, **k: (SimpleNamespace(system_prompt="sys"), "stable"),
    )
    monkeypatch.setattr(pipeline_module, "get_agent", lambda name: agent)


@pytest.mark.asyncio
async def test_ok_path(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    memory = FakeMemory()
    p = make_pipeline(memory=memory)
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "ok"
    assert result.text == "agent reply"
    assert result.state == "ROUTED"
    assert result.intent == "coding"
    assert result.need_human_review is False
    assert result.fallback == "regex"
    assert memory.added == [("s1", "hello", "agent reply")]
    assert agent.envelopes[0].agent_local_slot["intent"] == "coding"


@pytest.mark.asyncio
async def test_ok_path_need_human_review_from_evaluator(monkeypatch):
    patch_agents(monkeypatch, OkAgent())
    p = MoAPipeline(
        engine=Engine(),
        router=FakeRouter(),
        memory=FakeMemory(),
        adapter=ResponseAdapter(),
        evaluator=FakeEvaluator(need_human_review=True),
        retriever=FakeRetriever(),
        prompt_registry=object(),
        flag_client=FakeFlagClient(),
        guard_service=FakeGuard(GuardVerdict(action=GuardianAction.ALLOW, reason="ok")),
        command_mode=FakeCommandMode(),
    )
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "ok"
    assert result.need_human_review is True


@pytest.mark.asyncio
async def test_command_switch_mode(monkeypatch):
    cmd = FakeCommandMode()
    p = make_pipeline(command_mode=cmd)
    result = await p.run(make_event(text="/coding"), channel="test", target="s1")
    assert result.status == "command"
    assert result.intent == "coder"
    assert result.state == "ROUTED"
    assert result.text == "已切换至 编程 模式"
    assert cmd.store["s1"] == "coding"


@pytest.mark.asyncio
async def test_command_help(monkeypatch):
    p = make_pipeline()
    result = await p.run(make_event(text="/help"), channel="test", target="s1")
    assert result.status == "command"
    assert result.intent == "help"
    assert "可用指令" in result.text


@pytest.mark.asyncio
async def test_command_unknown(monkeypatch):
    p = make_pipeline()
    result = await p.run(make_event(text="/nope"), channel="test", target="s1")
    assert result.status == "command"
    assert result.intent == "help"
    assert "未知指令" in result.text


@pytest.mark.asyncio
async def test_command_mode_forces_intent(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    cmd = FakeCommandMode()
    cmd.store["s1"] = "coding"
    p = make_pipeline(router=FakeRouter(intent="assistant"), command_mode=cmd)
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.intent == "coding"
    assert agent.envelopes[0].agent_local_slot["intent"] == "coding"


@pytest.mark.asyncio
async def test_review_path_via_execution_marker(monkeypatch):
    class ExecuteCodeAgent:
        async def execute(self, envelope):
            return "EXECUTION_REQUIRES_APPROVAL: code=print('hi') lines=1"

    patch_agents(monkeypatch, ExecuteCodeAgent())
    engine = Engine()
    card_sender = FakeCardSender()
    guard = FakeGuard(GuardVerdict(action=GuardianAction.REVIEW, reason="needs approval"))
    p = make_pipeline(engine=engine, guard=guard, card_sender=card_sender)
    result = await p.run(make_event(), channel="test", target="t1")
    assert result.status == "pending_review"
    assert result.state == "SUSPENDED"
    assert result.text == "Output requires human approval before delivery"
    assert result.need_human_review is True
    stored = engine.session_store.get_hitl(result.trace_id)
    assert stored is not None
    assert "EXECUTION_REQUIRES_APPROVAL" in stored.agent_output
    assert stored.intent == "coding"
    assert stored.agent_name == "coder"
    assert stored.channel == "test"
    assert stored.target == "t1"
    assert len(card_sender.cards) == 1
    assert card_sender.cards[0].session_id == "s1"
    guard_intent = guard.calls[0][1]
    guard_hitl = guard.calls[0][3]
    assert guard_intent == "execute_code"
    assert guard_hitl is True


@pytest.mark.asyncio
async def test_review_without_marker_uses_intent_and_hitl_setting(monkeypatch):
    patch_agents(monkeypatch, OkAgent())
    guard = FakeGuard(GuardVerdict(action=GuardianAction.REVIEW, reason="needs approval"))
    p = make_pipeline(guard=guard)
    await p.run(make_event(), channel="test", target="t1")
    agent_name, guard_intent, payload, guard_hitl = guard.calls[0]
    assert agent_name == "coder"
    assert guard_intent == "coding"
    assert payload["role"] == "operator"
    assert payload["resource"] == "coding"


@pytest.mark.asyncio
async def test_review_via_real_guard_hitl_intent(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hitl_enabled", True)
    patch_agents(monkeypatch, OkAgent())
    engine = Engine()
    p = make_pipeline(
        engine=engine,
        router=FakeRouter(intent="write_file"),
        guard=GuardService(),
    )
    result = await p.run(make_event(), channel="test", target="t1")
    assert result.status == "pending_review"
    assert engine.session_store.get_hitl(result.trace_id) is not None


@pytest.mark.asyncio
async def test_review_same_session_twice_stores_both_by_trace_id(monkeypatch):
    class ExecuteCodeAgent:
        async def execute(self, envelope):
            return "EXECUTION_REQUIRES_APPROVAL: code=print('1')"

    patch_agents(monkeypatch, ExecuteCodeAgent())
    engine = Engine()
    guard = FakeGuard(GuardVerdict(action=GuardianAction.REVIEW, reason="needs approval"))
    p = make_pipeline(engine=engine, guard=guard)
    first = await p.run(make_event(session_id="s1"), channel="test", target="t1")
    second = await p.run(make_event(session_id="s1"), channel="test", target="t1")
    assert first.trace_id != second.trace_id
    first_stored = engine.session_store.get_hitl(first.trace_id)
    second_stored = engine.session_store.get_hitl(second.trace_id)
    assert first_stored is not None
    assert second_stored is not None
    assert first_stored.trace_id == first.trace_id
    assert second_stored.trace_id == second.trace_id


@pytest.mark.asyncio
async def test_deny_path(monkeypatch):
    patch_agents(monkeypatch, OkAgent())
    guard = FakeGuard(GuardVerdict(action=GuardianAction.DENY, reason="blocked by policy"))
    p = make_pipeline(guard=guard)
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "blocked"
    assert result.text == "blocked by policy"
    assert result.state == "ROUTED"


@pytest.mark.asyncio
async def test_deny_via_real_guard_sensitive_resource(monkeypatch):
    patch_agents(monkeypatch, OkAgent())
    p = make_pipeline(router=FakeRouter(intent="guard"), guard=GuardService())
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "blocked"
    assert "admin" in result.text


@pytest.mark.asyncio
async def test_agent_error_returns_error_without_raising(monkeypatch):
    patch_agents(monkeypatch, RaisingAgent())
    p = make_pipeline()
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "error"
    assert result.text == "agent execution failed"
    assert result.intent == "coding"


@pytest.mark.asyncio
async def test_log_request_skipped_without_request(monkeypatch):
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(pipeline_module, "log_request", fake_log)
    patch_agents(monkeypatch, OkAgent())
    p = make_pipeline()
    await p.run(make_event(), channel="test", target="s1")
    assert calls == []


@pytest.mark.asyncio
async def test_log_request_called_with_request(monkeypatch):
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(pipeline_module, "log_request", fake_log)
    patch_agents(monkeypatch, OkAgent())
    p = make_pipeline()
    req = SimpleNamespace(method="POST", url="http://test/x")
    result = await p.run(make_event(), channel="test", target="s1", request=req)
    assert result.status == "ok"
    assert len(calls) == 1
    call = calls[0]
    assert call[0] is req
    assert call[1] == 200
    assert call[4] == "coder"
    assert call[5] == "coding"
    assert call[6] == "allow"
    assert call[7] == "hello"
    assert call[8] == "agent reply"


@pytest.mark.asyncio
async def test_log_request_on_agent_error_logs_500(monkeypatch):
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(pipeline_module, "log_request", fake_log)
    patch_agents(monkeypatch, RaisingAgent())
    p = make_pipeline()
    req = SimpleNamespace(method="POST", url="http://test/x")
    result = await p.run(make_event(), channel="test", target="s1", request=req)
    assert result.status == "error"
    assert len(calls) == 1
    assert calls[0][1] == 500
    assert calls[0][6] == "error"
    assert calls[0][8] == "agent execution failed"


@pytest.mark.asyncio
async def test_command_logs_with_request(monkeypatch):
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(pipeline_module, "log_request", fake_log)
    p = make_pipeline()
    req = SimpleNamespace(method="POST", url="http://test/x")
    result = await p.run(make_event(text="/help"), channel="test", target="s1", request=req)
    assert result.status == "command"
    assert len(calls) == 1
    assert calls[0][1] == 200
    assert calls[0][4] == "command"
    assert calls[0][5] == "help"


@pytest.mark.asyncio
async def test_set_card_sender(monkeypatch):
    patch_agents(monkeypatch, OkAgent())
    sender = FakeCardSender()
    p = make_pipeline()
    p.set_card_sender(sender)
    assert p.card_sender is sender
