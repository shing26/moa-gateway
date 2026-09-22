from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.pipeline as pipeline_module
from app.engine import Engine
from app.evaluator.evaluator import EvalResult
from app.fsm.state_machine import Event as FsmEvent
from app.guard.rbac import GuardianAction, GuardVerdict
from app.long_term_memory import LongTermMemory
from app.models.events import MoAEvent, new_trace_id
from app.outbound.adapter import ResponseAdapter
from app.pipeline import MoAPipeline
from app.vectordb import VectorDBClient
from app.vectordb.retriever import RetrievalResult


class FakeRetriever:
    def __init__(self, context="retrieved context"):
        self.context = context

    async def retrieve(self, query, session_id=None, user_id=None):
        return RetrievalResult(chunks=[], context=self.context, doc_count=0)


class FakeFlagClient:
    async def get(self, name, default=False):
        return False


class FakeEvaluator:
    async def score(self, output_text, intent):
        # 同 test_pipeline：桩返回真实 EvalResult，避免少字段的漂移
        return EvalResult(score=1.0, need_human_review=False)


class FakeMemory:
    def get_history(self, session_id):
        return []

    def add(self, session_id, user_msg, assistant_msg):
        pass

    def clear(self, session_id):
        pass


class FakeRouter:
    async def route(self, text):
        return ("coding", "regex")


class FakeCommandMode:
    def set(self, session_id, mode):
        pass

    def get(self, session_id):
        return None

    def clear(self, session_id):
        pass


class FakeGuard:
    def evaluate(self, agent_name, intent, payload, *, hitl_enabled=True):
        return GuardVerdict(action=GuardianAction.ALLOW, reason="ok")


class RecordingAgent:
    def __init__(self):
        self.envelopes = []

    async def execute(self, envelope):
        self.envelopes.append(envelope)
        return "agent reply"


def make_pipeline(long_term_memory=None, agent=None, retriever=None):
    return MoAPipeline(
        engine=Engine(),
        router=FakeRouter(),
        memory=FakeMemory(),
        adapter=ResponseAdapter(),
        evaluator=FakeEvaluator(),
        retriever=retriever or FakeRetriever(),
        prompt_registry=object(),
        flag_client=FakeFlagClient(),
        guard_service=FakeGuard(),
        command_mode=FakeCommandMode(),
        long_term_memory=long_term_memory,
    )


def make_event(text, session_id, user_id=""):
    return MoAEvent(
        trace_id=new_trace_id(),
        event=FsmEvent.MESSAGE_RECEIVED,
        session_id=session_id,
        text=text,
        context={"source": "test"},
        user_id=user_id,
    )


def patch_agents(monkeypatch, agent):
    monkeypatch.setattr(
        pipeline_module,
        "select_canary_version",
        lambda *a, **k: (SimpleNamespace(system_prompt="sys"), "stable"),
    )
    monkeypatch.setattr(pipeline_module, "get_agent", lambda name: agent)


@pytest.mark.asyncio
async def test_recalls_fact_from_previous_session(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    ltm = LongTermMemory(VectorDBClient())
    p = make_pipeline(long_term_memory=ltm)

    await p.run(make_event("记住我的名字是小张", "s1", "u1"), channel="test", target="s1")
    await p.run(make_event("我的名字是什么", "s2", "u1"), channel="test", target="s2")

    assert "小张" in agent.envelopes[-1].global_summary


@pytest.mark.asyncio
async def test_memory_isolated_between_users(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    ltm = LongTermMemory(VectorDBClient())
    p = make_pipeline(long_term_memory=ltm)

    await p.run(make_event("记住我的名字是小张", "s1", "u1"), channel="test", target="s1")
    await p.run(make_event("我的名字是什么", "s2", "u2"), channel="test", target="s2")

    assert "小张" not in agent.envelopes[-1].global_summary


@pytest.mark.asyncio
async def test_forget_removes_fact_from_later_recall(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    ltm = LongTermMemory(VectorDBClient())
    p = make_pipeline(long_term_memory=ltm)

    await p.run(make_event("记住我的名字是小张", "s1", "u1"), channel="test", target="s1")
    await p.run(make_event("忘掉我的名字", "s2", "u1"), channel="test", target="s2")
    await p.run(make_event("我的名字是什么", "s3", "u1"), channel="test", target="s3")

    assert "小张" not in agent.envelopes[-1].global_summary
    assert await ltm.list_for("u1") == []


@pytest.mark.asyncio
async def test_no_long_term_memory_keeps_previous_behaviour(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    p = make_pipeline(long_term_memory=None)

    await p.run(make_event("hello", "s1", "u1"), channel="test", target="s1")

    # 未注入长期记忆时，global_summary 仍是纯检索上下文，不含额外拼接。
    assert agent.envelopes[-1].global_summary == "retrieved context"


@pytest.mark.asyncio
async def test_event_user_id_falls_back_to_context(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    ltm = LongTermMemory(VectorDBClient())
    p = make_pipeline(long_term_memory=ltm)

    event = MoAEvent(
        trace_id=new_trace_id(),
        event=FsmEvent.MESSAGE_RECEIVED,
        session_id="s1",
        text="记住我的名字是小张",
        context={"source": "test", "user_id": "ctx-user"},
    )
    await p.run(event, channel="test", target="s1")

    assert len(await ltm.list_for("ctx-user")) == 1
