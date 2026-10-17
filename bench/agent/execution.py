"""子任务执行结果的共享类型与 reducer。"""

from __future__ import annotations

from dataclasses import dataclass, replace

from bench.agent.types import AgentStatus, Step, SubtaskUsage, Usage


@dataclass(frozen=True)
class SubtaskOutcome:
    """一个子任务的产出：给 Critic 看的结论加压缩轨迹。"""

    index: int
    instruction: str
    output: str
    trajectory: str


@dataclass(frozen=True)
class SubtaskExecution:
    """一次"跑一个或多个子任务"的结果集。"""

    outcomes: tuple[SubtaskOutcome, ...]
    usage: Usage
    turns: int
    steps: tuple[Step, ...]
    model: str
    status: AgentStatus
    subtask_usage: tuple[SubtaskUsage, ...] = ()
    error: str = ""


def ordered_outcomes(
    by_index: dict[int, SubtaskOutcome],
) -> tuple[SubtaskOutcome, ...]:
    """按子任务下标还原稳定顺序。"""
    return tuple(by_index[index] for index in sorted(by_index))


def merge_execution(
    left: SubtaskExecution,
    right: SubtaskExecution,
) -> SubtaskExecution:
    """并发结果 reducer：同下标覆盖，其他下标保留，账本累加。"""
    outcomes = {item.index: item for item in left.outcomes}
    for item in right.outcomes:
        outcomes[item.index] = item
    usage_by_index = {item.index: item for item in left.subtask_usage}
    for item in right.subtask_usage:
        usage_by_index[item.index] = item
    return SubtaskExecution(
        outcomes=ordered_outcomes(outcomes),
        usage=left.usage + right.usage,
        turns=left.turns + right.turns,
        steps=(*left.steps, *right.steps),
        model=right.model or left.model,
        status=_merge_status(left.status, right.status),
        subtask_usage=tuple(usage_by_index[index] for index in sorted(usage_by_index)),
        error=right.error or left.error,
    )


def with_status(execution: SubtaskExecution, status: AgentStatus) -> SubtaskExecution:
    return replace(execution, status=status)


def _merge_status(left: AgentStatus, right: AgentStatus) -> AgentStatus:
    """并发 wave 的失败优先级：基础设施问题压过预算，再压过步数。"""
    rank = {
        "completed": 0,
        "step_limit": 1,
        "budget_exhausted": 2,
        "error": 3,
    }
    return left if rank[left] >= rank[right] else right


__all__ = [
    "SubtaskExecution",
    "SubtaskOutcome",
    "merge_execution",
    "ordered_outcomes",
    "with_status",
]
