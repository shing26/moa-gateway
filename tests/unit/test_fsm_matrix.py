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


def test_request_lifecycle_transitions():
    """ADR-010：执行期主干（进入执行 / 交付前刹车 / 交付完成）必须真的可达。"""
    assert next_state(State.ROUTED, Event.TASK_STARTED) == State.EXECUTING
    # 挂起中再来一条消息：新请求仍会执行（同会话可累积多个待审批，按 trace 区分）
    assert next_state(State.SUSPENDED, Event.TASK_STARTED) == State.EXECUTING
    # guard / 评估器在交付前刹车
    assert next_state(State.OUTPUT_READY, Event.NEEDS_HUMAN) == State.SUSPENDED
    assert next_state(State.OUTPUT_READY, Event.DELIVERED) == State.COMPLETED


def test_every_state_is_reachable():
    """8 个状态都必须有真实入边。

    ADR-010 之前 RETRY / OUTPUT_READY / COMPLETED 没有任何发出者，是纯装饰状态
    ——"FSM 8 态"这个说法当时只成立 5 个。这条守卫让那种情况不能再悄悄回来。
    """
    reachable = {State.INIT}
    changed = True
    while changed:
        changed = False
        for (src, _event), dst in TRANSITIONS.items():
            if src in reachable and dst not in reachable:
                reachable.add(dst)
                changed = True
    assert reachable == set(State), f"不可达状态: {sorted(s.value for s in set(State) - reachable)}"
