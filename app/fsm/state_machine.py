from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    INIT = "INIT"
    ROUTED = "ROUTED"
    SUSPENDED = "SUSPENDED"
    EXECUTING = "EXECUTING"
    OUTPUT_READY = "OUTPUT_READY"
    RETRY = "RETRY"
    REJECTED = "REJECTED"
    COMPLETED = "COMPLETED"


class Event(str, Enum):
    MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
    SENSITIVE_DETECTED = "SENSITIVE_DETECTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    TASK_SUCCESS = "TASK_SUCCESS"
    TASK_FAILED = "TASK_FAILED"
    CANCEL = "CANCEL"
    RESET = "RESET"
    FALLBACK_APPLIED = "FALLBACK_APPLIED"


TRANSITIONS: dict[tuple[State, Event], State] = {
    (State.INIT, Event.MESSAGE_RECEIVED): State.ROUTED,
    (State.INIT, Event.RESET): State.INIT,
    (State.ROUTED, Event.SENSITIVE_DETECTED): State.SUSPENDED,
    (State.ROUTED, Event.NEEDS_HUMAN): State.SUSPENDED,
    (State.SUSPENDED, Event.HUMAN_APPROVED): State.EXECUTING,
    (State.SUSPENDED, Event.HUMAN_REJECTED): State.REJECTED,
    (State.EXECUTING, Event.TASK_SUCCESS): State.OUTPUT_READY,
    (State.EXECUTING, Event.TASK_FAILED): State.RETRY,
    (State.RETRY, Event.TASK_SUCCESS): State.OUTPUT_READY,
    (State.RETRY, Event.TASK_FAILED): State.SUSPENDED,
    (State.OUTPUT_READY, Event.RESET): State.INIT,
}


@dataclass
class StateContext:
    state: State = State.INIT
    session_id: str | None = None
    trace_id: str | None = None
    retry_count: int = 0
    metadata: dict[str, object] = field(default_factory=dict)


class InvalidStateTransitionException(Exception):
    pass


def next_state(current: State, event: Event) -> State:
    key = (current, event)
    if key not in TRANSITIONS:
        raise InvalidStateTransitionException(f"Invalid transition: {current} + {event}")
    return TRANSITIONS[key]
