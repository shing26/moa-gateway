from __future__ import annotations

import pytest

from app.engine import Engine
from app.fsm.state_machine import Event, State
from app.models.events import MoAEvent


def _event(session_id: str, event: Event, text: str = "") -> MoAEvent:
    return MoAEvent(
        trace_id=f"trace-{session_id}-{event.value}",
        event=event,
        session_id=session_id,
        text=text,
        context={},
    )


@pytest.mark.asyncio
async def test_engine_persists_state_across_events() -> None:
    engine = Engine()
    first = await engine.handle_event(_event("s1", Event.MESSAGE_RECEIVED))
    assert first.context.state == State.ROUTED

    second = await engine.handle_event(_event("s1", Event.MESSAGE_RECEIVED))
    assert second.context.state == State.ROUTED

    suspended = await engine.handle_event(_event("s1", Event.SENSITIVE_DETECTED))
    assert suspended.context.state == State.SUSPENDED
    assert suspended.context.metadata.get("state_stack") == ["SUSPENDED"]
    assert suspended.context.metadata.get("sensitive_pending") is True

    reset = await engine.handle_event(_event("s1", Event.RESET))
    assert reset.context.state == State.INIT
    assert reset.state_stack == []
    assert reset.context.metadata.get("hitl_pending") is False
    assert reset.context.metadata.get("sensitive_pending") is False

    resumed = await engine.handle_event(_event("s1", Event.MESSAGE_RECEIVED))
    assert resumed.context.state == State.ROUTED


@pytest.mark.asyncio
async def test_engine_reset_session_clears_persisted_state() -> None:
    engine = Engine()
    await engine.handle_event(_event("s2", Event.SENSITIVE_DETECTED))
    engine.reset_session("s2")

    fresh = await engine.handle_event(_event("s2", Event.MESSAGE_RECEIVED))
    assert fresh.context.state == State.ROUTED


@pytest.mark.asyncio
async def test_engine_cancel_resets_from_suspended() -> None:
    engine = Engine()
    await engine.handle_event(_event("s3", Event.SENSITIVE_DETECTED))
    cancelled = await engine.handle_event(_event("s3", Event.CANCEL))
    assert cancelled.context.state == State.INIT
