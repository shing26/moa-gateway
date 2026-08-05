import pytest

from app.fsm.state_machine import (
    Event,
    InvalidStateTransitionException,
    State,
    TRANSITIONS,
    next_state,
)


def test_adr_matrix():
    assert next_state(State.INIT, Event.MESSAGE_RECEIVED) == State.ROUTED
    assert next_state(State.ROUTED, Event.SENSITIVE_DETECTED) == State.SUSPENDED
    assert next_state(State.SUSPENDED, Event.HUMAN_APPROVED) == State.EXECUTING
    assert next_state(State.EXECUTING, Event.TASK_SUCCESS) == State.OUTPUT_READY
    assert next_state(State.EXECUTING, Event.TASK_FAILED) == State.RETRY
    assert next_state(State.RETRY, Event.TASK_SUCCESS) == State.OUTPUT_READY
    assert next_state(State.RETRY, Event.TASK_FAILED) == State.SUSPENDED


def test_adr_rejects_illegal_moves():
    with pytest.raises(InvalidStateTransitionException):
        next_state(State.INIT, Event.HUMAN_APPROVED)
    with pytest.raises(InvalidStateTransitionException):
        next_state(State.ROUTED, Event.TASK_SUCCESS)


def test_needs_human_transition():
    assert next_state(State.ROUTED, Event.NEEDS_HUMAN) == State.SUSPENDED
    assert next_state(State.SUSPENDED, Event.HUMAN_APPROVED) == State.EXECUTING
    assert next_state(State.SUSPENDED, Event.HUMAN_REJECTED) == State.REJECTED


def test_reset_from_init():
    assert next_state(State.INIT, Event.RESET) == State.INIT


def test_sensitive_detected_new_transitions():
    assert next_state(State.INIT, Event.SENSITIVE_DETECTED) == State.SUSPENDED
    assert next_state(State.SUSPENDED, Event.SENSITIVE_DETECTED) == State.SUSPENDED
    assert next_state(State.EXECUTING, Event.SENSITIVE_DETECTED) == State.SUSPENDED
    assert next_state(State.RETRY, Event.SENSITIVE_DETECTED) == State.SUSPENDED


def test_sensitive_detected_matrix_never_raises():
    sensitive = [(s, e) for (s, e) in TRANSITIONS if e == Event.SENSITIVE_DETECTED]
    assert sensitive
    for (s, e) in sensitive:
        assert next_state(s, e) == TRANSITIONS[(s, e)]
