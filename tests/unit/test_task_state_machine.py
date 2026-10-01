"""任务状态机的不变式（D3）。

这些断言保护的是**并发归属**，不是路由正确性：一旦 worker 跑起来，"谁在什么状态
下能动"就成了正确性的地基。表少一条边，表现是"某个动作悄悄没发生"而不是报错。
"""

from __future__ import annotations

import pytest

from apps.code_review_pipeline.task_state import (
    ACTION_TARGET,
    ALLOWED_ACTIONS,
    InvalidTaskTransition,
    TaskAction,
    TaskState,
    can,
    initial_transition,
    is_terminal,
    next_state,
)


def test_happy_path_is_walkable() -> None:
    """计划里的主链路必须走得通。"""
    state = TaskState.QUEUED
    path = []
    for action in (
        TaskAction.CLAIM,
        TaskAction.REQUEST_APPROVAL,
        TaskAction.APPROVE,
        TaskAction.COMPLETE,
    ):
        state = next_state(state, action)
        path.append(state.value)
    assert path == ["running", "waiting_approval", "posting", "done"]
    assert is_terminal(state)


@pytest.mark.parametrize("terminal", [TaskState.DONE, TaskState.REJECTED])
def test_terminal_states_allow_nothing(terminal: TaskState) -> None:
    """终态必须拒绝**每一个**动作，不只是拒绝 claim。

    少断言一个动作，等于把"重复投递能否重跑"这个幂等问题漏回调用点自己判断——
    而调用点正是最容易漏的地方。
    """
    assert ALLOWED_ACTIONS[terminal] == frozenset()
    for action in TaskAction:
        assert not can(terminal, action), f"{terminal.value} 不该允许 {action.value}"
        with pytest.raises(InvalidTaskTransition):
            next_state(terminal, action)


def test_failed_only_requeues_never_claims() -> None:
    """failed 不允许直接 claim：重入必须留痕。

    若允许 failed --claim--> running，这次重入在 state_transitions 里与首次运行
    完全一样，事后无法区分"跑过一次"和"跑过三次"。
    """
    assert can(TaskState.FAILED, TaskAction.REQUEUE)
    assert not can(TaskState.FAILED, TaskAction.CLAIM)
    with pytest.raises(InvalidTaskTransition):
        next_state(TaskState.FAILED, TaskAction.CLAIM)
    assert next_state(TaskState.FAILED, TaskAction.REQUEUE) is TaskState.QUEUED


def test_reject_terminates_and_is_not_done() -> None:
    """人工拒绝必须是独立终态，不能与 done 混同。

    done 的含义是"已成功写回 GitHub"，它的幂等判断是 posted_review_id 非空。
    若拒绝也落到 done，demo 里会出现"人拒绝了但系统显示已完成"。
    """
    rejected = next_state(TaskState.WAITING_APPROVAL, TaskAction.REJECT)
    assert rejected is TaskState.REJECTED
    assert rejected is not TaskState.DONE
    assert is_terminal(rejected)


def test_waiting_approval_accepts_human_decisions() -> None:
    """挂起中必须仍能 approve / reject：worker 崩了不能把人锁死。"""
    for action in (TaskAction.APPROVE, TaskAction.REJECT, TaskAction.FAIL):
        assert can(TaskState.WAITING_APPROVAL, action), action.value


def test_no_action_is_dead_vocabulary() -> None:
    """每个动作都必须至少被一个状态接受。

    反向漂移守卫：出现一个没人用的动作（如曾经的 start_posting）说明词汇表
    膨胀了——没有消费者的取值只会误导后来的人。
    """
    accepted = {a for actions in ALLOWED_ACTIONS.values() for a in actions}
    assert accepted == set(TaskAction), (
        f"没有被任何状态接受的动作: {sorted(a.value for a in set(TaskAction) - accepted)}"
    )


def test_every_allowed_action_has_a_target() -> None:
    """允许集合里的每个动作都必须在 ACTION_TARGET 里有目标状态。"""
    accepted = {a for actions in ALLOWED_ACTIONS.values() for a in actions}
    assert accepted <= set(ACTION_TARGET)


def test_no_state_is_unreachable() -> None:
    """每个状态都必须能由某个动作到达。"""
    reachable = {TaskState.QUEUED, *(ACTION_TARGET[a] for a in TaskAction)}
    assert reachable == set(TaskState), (
        f"无法到达的状态: {sorted(s.value for s in set(TaskState) - reachable)}"
    )


def test_non_terminal_states_always_have_a_way_out() -> None:
    """所有非终态都必须还有出路，否则任务会卡死且无人察觉。"""
    for state in TaskState:
        if is_terminal(state):
            continue
        assert ALLOWED_ACTIONS[state], f"{state.value} 非终态却没有可用动作"


def test_transition_error_names_state_and_action() -> None:
    """错误信息要说清谁被拒了，便于从 worker 日志直接定位。"""
    with pytest.raises(InvalidTaskTransition) as exc:
        next_state(TaskState.RUNNING, TaskAction.APPROVE)
    assert exc.value.state is TaskState.RUNNING
    assert exc.value.action is TaskAction.APPROVE
    assert "running" in str(exc.value) and "approve" in str(exc.value)


def test_initial_transition_records_queued_with_timestamp() -> None:
    rows = initial_transition("2026-10-01T00:00:00+00:00")
    assert rows == [{"from": None, "to": "queued", "at": "2026-10-01T00:00:00+00:00"}]
