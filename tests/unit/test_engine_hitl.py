from __future__ import annotations

import pytest

from app.engine import Engine, HitlRequest
from app.fsm.state_machine import Event, State, next_state


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
    engine.store_hitl("sess-1", req)
    retrieved = engine.get_hitl("sess-1")
    assert retrieved is not None
    assert retrieved.agent_output == "confidential data"


@pytest.mark.asyncio
async def test_engine_removes_hitl(engine: Engine):
    req = HitlRequest(
        session_id="sess-2", trace_id="trace-2", agent_output="data",
        intent="assistant", agent_name="general", channel="feishu", target="chat_456",
    )
    engine.store_hitl("sess-2", req)
    engine.remove_hitl("sess-2")
    assert engine.get_hitl("sess-2") is None


@pytest.mark.asyncio
async def test_engine_get_hitl_returns_none_for_unknown(engine: Engine):
    assert engine.get_hitl("nonexistent") is None


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
