"""子任务执行结果的共享类型与 reducer。

reducer 的两条规则不同，别把二者混为一谈：

* **产出**（``outcomes``）同下标**覆盖**——重跑一个子任务后，Critic 该看的是最新那条。
* **账本**（``subtask_usage``）同下标**累加**——一个子任务跑了几轮，它花掉的钱是几轮
  之和。若这里也覆盖，前一版的 ``Usage`` 就只剩总量、再也归因不到是哪个 worker 烧的
  （§6 的"每个 worker 的用量可归因"）。
"""

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
    """并发结果 reducer：产出同下标覆盖，账本同下标累加，其他下标保留。"""
    outcomes = {item.index: item for item in left.outcomes}
    for item in right.outcomes:
        outcomes[item.index] = item
    return SubtaskExecution(
        outcomes=ordered_outcomes(outcomes),
        usage=left.usage + right.usage,
        turns=left.turns + right.turns,
        steps=(*left.steps, *right.steps),
        model=right.model or left.model,
        status=_merge_status(left.status, right.status),
        subtask_usage=merge_subtask_usage(left.subtask_usage, right.subtask_usage),
        error=right.error or left.error,
    )


def merge_subtask_usage(
    left: tuple[SubtaskUsage, ...],
    right: tuple[SubtaskUsage, ...],
) -> tuple[SubtaskUsage, ...]:
    """按子任务下标合并账本切片：新到的下标追加，同下标的两段用量相加。

    一个子任务在回边里被重跑时携带的是**新一轮**的用量，而不是它到此为止的总量；
    所以同下标要相加，否则重跑会把前一轮的花费从切片里挤掉。
    """
    merged = {item.index: item for item in left}
    for item in right:
        merged[item.index] = accumulate_subtask_usage(merged[item.index], item) if (
            item.index in merged
        ) else item
    return tuple(merged[index] for index in sorted(merged))


def accumulate_subtask_usage(prior: SubtaskUsage, fresh: SubtaskUsage) -> SubtaskUsage:
    """把同一子任务的新一轮账本压进旧切片：用量与轮数相加，状态/模型取最新。"""
    return replace(
        fresh,
        usage=prior.usage + fresh.usage,
        turns=prior.turns + fresh.turns,
    )


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
    "accumulate_subtask_usage",
    "SubtaskExecution",
    "SubtaskOutcome",
    "merge_execution",
    "merge_subtask_usage",
    "ordered_outcomes",
]
