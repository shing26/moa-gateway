from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.deps import pipeline
import app.pipeline as pipeline_module
from app.engine import Engine, HitlRequest
from app.fsm.state_machine import Event, State, next_state
from app.main import app
from app.vectordb.retriever import RetrievalResult


@pytest.fixture
def engine() -> Engine:
    return Engine()


# ── HITL storage tests (engine level) ──────────────────────────────────

@pytest.mark.asyncio
async def test_engine_stores_and_retrieves_hitl(engine: Engine):
    req = HitlRequest(
        session_id="sess-1", trace_id="trace-1", agent_output="confidential data",
        intent="write_file", agent_name="coder", channel="feishu", target="chat_123",
    )
    engine.session_store.store_hitl("sess-1", req)
    retrieved = engine.session_store.get_hitl("sess-1")
    assert retrieved is not None
    assert retrieved.agent_output == "confidential data"


@pytest.mark.asyncio
async def test_engine_removes_hitl(engine: Engine):
    req = HitlRequest(
        session_id="sess-2", trace_id="trace-2", agent_output="data",
        intent="assistant", agent_name="general", channel="feishu", target="chat_456",
    )
    engine.session_store.store_hitl("sess-2", req)
    engine.session_store.remove_hitl("sess-2")
    assert engine.session_store.get_hitl("sess-2") is None


@pytest.mark.asyncio
async def test_engine_get_hitl_returns_none_for_unknown(engine: Engine):
    assert engine.session_store.get_hitl("nonexistent") is None


# ── FSM-level HITL state transitions ───────────────────────────────────
# These are tested at the FSM level because engine.handle_event() creates
# a fresh StateContext on every call (session state is managed externally).


class TestHitlFsmTransitions:
    def test_routed_plus_needs_human_goes_to_suspended(self):
        assert next_state(State.ROUTED, Event.NEEDS_HUMAN) == State.SUSPENDED

    def test_suspended_plus_human_approved_goes_to_executing(self):
        assert next_state(State.SUSPENDED, Event.HUMAN_APPROVED) == State.EXECUTING

    def test_suspended_plus_human_rejected_goes_to_rejected(self):
        assert next_state(State.SUSPENDED, Event.HUMAN_REJECTED) == State.REJECTED


# ── Webhook-level REVIEW trigger via EXECUTION_REQUIRES_APPROVAL ──────

def test_webhook_execute_code_marker_triggers_review(monkeypatch) -> None:
    class ExecuteCodeAgent:
        async def execute(self, envelope):
            return "EXECUTION_REQUIRES_APPROVAL: code=print('hi') lines=1"

    async def fake_rate(key):
        return (True, 10)

    async def fake_handle(event):
        return SimpleNamespace(context=SimpleNamespace(state=SimpleNamespace(value="ROUTED")))

    async def fake_route(text):
        return ("coding", False)

    async def fake_retrieve(*args, **kwargs):
        return RetrievalResult(chunks=[], context="", doc_count=0)

    async def fake_flag(*args, **kwargs):
        return False

    async def fake_score(*args, **kwargs):
        return SimpleNamespace(score=1.0, need_human_review=False)

    async def fake_log(*args, **kwargs):
        pass

    import app.routes.webhook as webhook_route

    agent = ExecuteCodeAgent()
    monkeypatch.setattr(webhook_route.rate_limiter, "check", fake_rate)
    monkeypatch.setattr(pipeline.engine, "handle_event", fake_handle)
    monkeypatch.setattr(pipeline.command_mode, "get", lambda sid: None)
    monkeypatch.setattr(pipeline.router, "route", fake_route)
    monkeypatch.setattr(pipeline_module, "get_agent", lambda name: agent)
    monkeypatch.setattr(pipeline.retriever, "retrieve", fake_retrieve)
    monkeypatch.setattr(pipeline.flag_client, "get", fake_flag)
    monkeypatch.setattr(
        pipeline_module,
        "select_canary_version",
        lambda *a, **k: (SimpleNamespace(system_prompt=""), "stable"),
    )
    monkeypatch.setattr(pipeline.evaluator, "score", fake_score)
    monkeypatch.setattr(pipeline_module, "log_request", fake_log)

    with TestClient(app) as client:
        res = client.post(
            "/webhook/feishu",
            json={"session_id": "hitl-sess", "chat_id": "c1", "text": "帮我执行这段代码"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["state"] == "SUSPENDED"
        assert body["status"] == "pending_review"
        assert body["intent"] == "coding"

        stored = pipeline.engine.session_store.get_hitl("hitl-sess")
        assert stored is not None
        assert "EXECUTION_REQUIRES_APPROVAL" in stored.agent_output
        assert stored.intent == "coding"
        assert stored.agent_name == "coder"
        pipeline.engine.session_store.remove_hitl("hitl-sess")
