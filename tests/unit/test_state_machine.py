import pytest

from app.fsm.state_machine import (
    Event,
    InvalidStateTransitionException,
    State,
    StateContext,
    next_state,
)


def test_initial_state_is_init():
    assert State.INIT.value == "INIT"


def test_known_transition_moves_state():
    assert next_state(State.INIT, Event.MESSAGE_RECEIVED) == State.ROUTED
    assert next_state(State.ROUTED, Event.SENSITIVE_DETECTED) == State.SUSPENDED
    assert next_state(State.SUSPENDED, Event.HUMAN_APPROVED) == State.EXECUTING
    assert next_state(State.EXECUTING, Event.TASK_SUCCESS) == State.OUTPUT_READY


def test_invalid_transition_raises():
    with pytest.raises(InvalidStateTransitionException):
        next_state(State.INIT, Event.HUMAN_APPROVED)


def test_state_context_defaults():
    ctx = StateContext()
    assert ctx.state == State.INIT
    assert ctx.retry_count == 0
    assert ctx.metadata == {}
