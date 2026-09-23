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
    TASK_STARTED = "TASK_STARTED"
    TASK_SUCCESS = "TASK_SUCCESS"
    TASK_FAILED = "TASK_FAILED"
    DELIVERED = "DELIVERED"
    CANCEL = "CANCEL"
    RESET = "RESET"


TRANSITIONS: dict[tuple[State, Event], State] = {
    (State.INIT, Event.MESSAGE_RECEIVED): State.ROUTED,
    (State.ROUTED, Event.MESSAGE_RECEIVED): State.ROUTED,
    (State.EXECUTING, Event.MESSAGE_RECEIVED): State.ROUTED,
    (State.RETRY, Event.MESSAGE_RECEIVED): State.ROUTED,
    (State.OUTPUT_READY, Event.MESSAGE_RECEIVED): State.ROUTED,
    (State.REJECTED, Event.MESSAGE_RECEIVED): State.ROUTED,
    (State.COMPLETED, Event.MESSAGE_RECEIVED): State.ROUTED,
    (State.SUSPENDED, Event.MESSAGE_RECEIVED): State.SUSPENDED,
    (State.INIT, Event.RESET): State.INIT,
    (State.ROUTED, Event.SENSITIVE_DETECTED): State.SUSPENDED,
    (State.INIT, Event.SENSITIVE_DETECTED): State.SUSPENDED,
    (State.SUSPENDED, Event.SENSITIVE_DETECTED): State.SUSPENDED,
    (State.EXECUTING, Event.SENSITIVE_DETECTED): State.SUSPENDED,
    (State.RETRY, Event.SENSITIVE_DETECTED): State.SUSPENDED,
    (State.OUTPUT_READY, Event.SENSITIVE_DETECTED): State.SUSPENDED,
    (State.REJECTED, Event.SENSITIVE_DETECTED): State.SUSPENDED,
    (State.COMPLETED, Event.SENSITIVE_DETECTED): State.SUSPENDED,
    (State.ROUTED, Event.NEEDS_HUMAN): State.SUSPENDED,
    (State.OUTPUT_READY, Event.NEEDS_HUMAN): State.SUSPENDED,
    (State.SUSPENDED, Event.NEEDS_HUMAN): State.SUSPENDED,
    # 挂起中再来一条消息：MESSAGE_RECEIVED 在 SUSPENDED 上是自环（见上），但那条
    # 新请求仍会被执行——这是既有行为（同会话可累积多个待审批，按 trace 区分）。
    # 所以执行期入口在 SUSPENDED 上也要有边，否则新消息会撞非法迁移。
    (State.SUSPENDED, Event.TASK_STARTED): State.EXECUTING,
    (State.SUSPENDED, Event.HUMAN_APPROVED): State.EXECUTING,
    (State.SUSPENDED, Event.HUMAN_REJECTED): State.REJECTED,
    # 执行期语义：TASK_STARTED 只从 ROUTED 进入 EXECUTING（首次尝试），重试期间状态
    # 停在 RETRY。所以"重试几次"由这张表的结构决定——EXECUTING 只允许一跳 RETRY
    # （EXECUTING --TASK_FAILED--> RETRY --TASK_FAILED--> SUSPENDED），不是配置项。
    # 改预算 = 改表；tests/unit/test_agent_retry.py 的漂移守卫钉住它与 RETRY_BUDGET 一致。
    (State.ROUTED, Event.TASK_STARTED): State.EXECUTING,
    (State.EXECUTING, Event.TASK_SUCCESS): State.OUTPUT_READY,
    (State.EXECUTING, Event.TASK_FAILED): State.RETRY,
    (State.RETRY, Event.TASK_SUCCESS): State.OUTPUT_READY,
    (State.RETRY, Event.TASK_FAILED): State.SUSPENDED,
    (State.OUTPUT_READY, Event.DELIVERED): State.COMPLETED,
    (State.OUTPUT_READY, Event.RESET): State.INIT,
}

for _state in State:
    TRANSITIONS[(_state, Event.RESET)] = State.INIT
    TRANSITIONS[(_state, Event.CANCEL)] = State.INIT


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
