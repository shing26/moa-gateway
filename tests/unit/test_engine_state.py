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


@pytest.mark.asyncio
async def test_retry_count_persists_across_events_and_resets_on_new_request() -> None:
    """retry_count 必须跨事件累积（StateContext 每次都是新建的），并在新请求时归零。"""
    engine = Engine()
    await engine.handle_event(_event("s4", Event.MESSAGE_RECEIVED))
    await engine.handle_event(_event("s4", Event.TASK_STARTED))

    first_failure = await engine.handle_event(_event("s4", Event.TASK_FAILED))
    assert first_failure.context.state == State.RETRY
    assert first_failure.context.retry_count == 1

    second_failure = await engine.handle_event(_event("s4", Event.TASK_FAILED))
    assert second_failure.context.state == State.SUSPENDED
    assert second_failure.context.retry_count == 2, "计数必须跨事件累积，不能被重建清零"

    fresh = await engine.handle_event(_event("s4", Event.MESSAGE_RECEIVED))
    assert fresh.context.retry_count == 0, "重试预算是每个请求的，不是每个会话的"


@pytest.mark.asyncio
async def test_execution_phase_walks_the_full_lifecycle() -> None:
    """INIT→ROUTED→EXECUTING→OUTPUT_READY→COMPLETED 全部经真实事件走到。"""
    engine = Engine()
    routed = await engine.handle_event(_event("s5", Event.MESSAGE_RECEIVED))
    assert routed.context.state == State.ROUTED

    executing = await engine.handle_event(_event("s5", Event.TASK_STARTED))
    assert executing.context.state == State.EXECUTING

    ready = await engine.handle_event(_event("s5", Event.TASK_SUCCESS))
    assert ready.context.state == State.OUTPUT_READY

    done = await engine.handle_event(_event("s5", Event.DELIVERED))
    assert done.context.state == State.COMPLETED
