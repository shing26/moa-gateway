"""并发执行结果的 reducer：产出同下标覆盖、账本同下标累加，其他下标保留。"""

from __future__ import annotations

import pytest

from bench.agent.execution import (
    SubtaskExecution,
    SubtaskOutcome,
    merge_execution,
    merge_subtask_usage,
)
from bench.agent.types import Step, SubtaskUsage, Usage


def _execution(
    *,
    index: int,
    output: str,
    cost: float,
    status: str = "completed",
) -> SubtaskExecution:
    return SubtaskExecution(
        outcomes=(
            SubtaskOutcome(
                index=index,
                instruction=f"任务 {index}",
                output=output,
                trajectory=output,
            ),
        ),
        usage=Usage(llm_calls=1, cost_usd=cost),
        turns=1,
        steps=(Step(index=0, action="call_tool", tool="write_file"),),
        model=f"worker-{index}",
        status=status,  # type: ignore[arg-type]
        subtask_usage=(
            SubtaskUsage(
                index=index,
                usage=Usage(llm_calls=1, cost_usd=cost),
                turns=1,
                status=status,  # type: ignore[arg-type]
                model=f"worker-{index}",
            ),
        ),
    )


def test_merge_execution_overwrites_the_outcome_and_accumulates_the_ledger() -> None:
    left = merge_execution(
        _execution(index=0, output="v0", cost=0.01),
        _execution(index=1, output="old", cost=0.02),
    )

    merged = merge_execution(left, _execution(index=1, output="fixed", cost=0.05))

    # 产出取最新那条，旧的下标保留。
    assert [(item.index, item.output) for item in merged.outcomes] == [
        (0, "v0"),
        (1, "fixed"),
    ]
    assert [item.index for item in merged.subtask_usage] == [0, 1]
    # 账本累加：重跑不会把上一轮的用量从切片里挤掉。
    assert merged.subtask_usage[1].usage.cost_usd == pytest.approx(0.07)
    assert merged.subtask_usage[1].turns == 2
    # 切片之和与总量一致——账本对得上，才算"每个 worker 可归因"。
    assert sum(item.usage.cost_usd for item in merged.subtask_usage) == pytest.approx(
        merged.usage.cost_usd
    )
    assert merged.usage.cost_usd == pytest.approx(0.08)


def test_merge_execution_keeps_the_most_severe_status() -> None:
    merged = merge_execution(
        _execution(index=0, output="ok", cost=0.01),
        _execution(index=1, output="", cost=0.02, status="error"),
    )

    assert merged.status == "error"


def test_merge_subtask_usage_accumulates_same_index_and_appends_new_ones() -> None:
    merged = merge_subtask_usage(
        (SubtaskUsage(index=0, usage=Usage(cost_usd=0.01), turns=1, status="completed"),),
        (
            SubtaskUsage(index=0, usage=Usage(cost_usd=0.03), turns=2, status="completed"),
            SubtaskUsage(index=1, usage=Usage(cost_usd=0.04), turns=1, status="completed"),
        ),
    )

    assert [item.index for item in merged] == [0, 1]
    assert merged[0].usage.cost_usd == pytest.approx(0.04)
    assert merged[0].turns == 3
    assert merged[1].usage.cost_usd == pytest.approx(0.04)
