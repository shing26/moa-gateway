"""并发执行结果的 reducer：同下标覆盖，其他结果保留，账本累加。"""

from __future__ import annotations

import pytest

from bench.agent.execution import SubtaskExecution, SubtaskOutcome, merge_execution
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


def test_merge_execution_replaces_the_same_index_and_keeps_the_rest() -> None:
    left = merge_execution(
        _execution(index=0, output="v0", cost=0.01),
        _execution(index=1, output="old", cost=0.02),
    )

    merged = merge_execution(left, _execution(index=1, output="fixed", cost=0.05))

    assert [(item.index, item.output) for item in merged.outcomes] == [
        (0, "v0"),
        (1, "fixed"),
    ]
    assert [item.index for item in merged.subtask_usage] == [0, 1]
    assert merged.subtask_usage[1].usage.cost_usd == pytest.approx(0.05)
    assert merged.usage.cost_usd == pytest.approx(0.08)


def test_merge_execution_keeps_the_most_severe_status() -> None:
    merged = merge_execution(
        _execution(index=0, output="ok", cost=0.01),
        _execution(index=1, output="", cost=0.02, status="error"),
    )

    assert merged.status == "error"
