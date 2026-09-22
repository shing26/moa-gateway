"""审计 trace 贯通：一次请求的所有审计条目共享同一 trace（可关联、可回流）。

此前 request_logger 每条都用 new_trace_id()，review 与后续 hitl_approve/reject
分属两条互不关联的记录——人工决策无法按 trace 关联回触发它的那次请求。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.evaluator.evaluator import EvalResult
from app.middleware import request_logger
from app.middleware.request_logger import bind_trace, log_request
from app.models.events import MoAEvent, new_trace_id


class _Collector:
    def __init__(self) -> None:
        self.entries: list = []

    async def append(self, entry) -> None:
        self.entries.append(entry)


@pytest.fixture()
def collector(monkeypatch):
    c = _Collector()
    monkeypatch.setattr(request_logger, "_wal", c)
    return c


# ── logger 单元 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_request_uses_bound_trace(collector):
    bind_trace("trace-abc")
    await log_request(None, 200, 1.0, session_id="s1")
    assert collector.entries[-1].trace_id == "trace-abc"


@pytest.mark.asyncio
async def test_log_request_generates_trace_when_unbound(collector):
    bind_trace("")
    await log_request(None, 200, 1.0, session_id="s1")
    assert collector.entries[-1].trace_id  # 未绑定时仍有可用的新 trace


@pytest.mark.asyncio
async def test_rebind_changes_subsequent_entries(collector):
    bind_trace("t1")
    await log_request(None, 200, 1.0, session_id="s1")
    bind_trace("t2")
    await log_request(None, 200, 1.0, session_id="s1")
    assert [e.trace_id for e in collector.entries] == ["t1", "t2"]


# ── 引擎侧：请求 trace 写进审计 ─────────────────────────────────────────────


def _fakes():
    from app.guard.rbac import GuardianAction, GuardVerdict
    from app.vectordb.retriever import RetrievalResult

    class FakeRetriever:
        async def retrieve(self, query, session_id=None, user_id=None):
            return RetrievalResult(chunks=[], context="", doc_count=0)

    class FakeFlagClient:
        async def get(self, name, default=False):
            return False

    class FakeEvaluator:
        async def score(self, output_text, intent):
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

    class FakeAgent:
        async def execute(self, envelope):
            return "agent reply"

    return SimpleNamespace(
        retriever=FakeRetriever(),
        flag_client=FakeFlagClient(),
        evaluator=FakeEvaluator(),
        memory=FakeMemory(),
        router=FakeRouter(),
        command_mode=FakeCommandMode(),
        guard=FakeGuard(),
        agent=FakeAgent(),
    )


def _event(text: str, sid: str) -> MoAEvent:
    from app.fsm.state_machine import Event as FsmEvent

    return MoAEvent(
        trace_id=new_trace_id(),
        event=FsmEvent.MESSAGE_RECEIVED,
        session_id=sid,
        text=text,
        context={"source": "test"},
    )


@pytest.mark.asyncio
async def test_fsm_pipeline_writes_request_trace_to_audit(monkeypatch, collector):
    import app.pipeline as pipeline_module
    from app.engine import Engine
    from app.outbound.adapter import ResponseAdapter
    from app.pipeline import MoAPipeline

    f = _fakes()
    monkeypatch.setattr(
        pipeline_module,
        "select_canary_version",
        lambda *a, **k: (SimpleNamespace(system_prompt="sys"), "stable"),
    )
    monkeypatch.setattr(pipeline_module, "get_agent", lambda name: f.agent)

    pipeline = MoAPipeline(
        engine=Engine(),
        router=f.router,
        memory=f.memory,
        adapter=ResponseAdapter(),
        evaluator=f.evaluator,
        retriever=f.retriever,
        prompt_registry=object(),
        flag_client=f.flag_client,
        guard_service=f.guard,
        command_mode=f.command_mode,
    )
    event = _event("你好", "trace-session")
    # pipeline 的审计只在带 request 时写（与真实路由一致）
    await pipeline.run(event, channel="test", target="trace-session", request=object())

    assert collector.entries, "pipeline 成功路径应写审计"
    assert {e.trace_id for e in collector.entries} == {event.trace_id}


@pytest.mark.asyncio
async def test_engine_dispatcher_writes_request_trace_to_audit(collector):
    from app.orchestration.dispatch import EngineDispatcher
    from app.pipeline import PipelineResult

    class _Graph:
        async def run(self, event, *, channel, target):
            return PipelineResult(
                trace_id=event.trace_id, state="", intent="", text="graph reply", status="ok",
            )

    dispatcher = EngineDispatcher(fsm=object(), graph=_Graph())
    event = _event("你好", "dispatch-session")
    await dispatcher.run(event, channel="test", target="dispatch-session", request=object())

    assert collector.entries, "dispatcher 图路径应写审计"
    assert {e.trace_id for e in collector.entries} == {event.trace_id}
