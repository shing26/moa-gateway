from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from bench.experiment import (
    BatchRunner,
    CapacityPilotObservation,
    ExperimentStore,
    RunMetrics,
    TrialEvidence,
    evaluate_capacity_gate,
    load_capacity_gate,
    load_protocol,
)

PROTOCOL_PATH = (
    Path(__file__).resolve().parents[2]
    / "bench"
    / "protocols"
    / "multi-agent-first-batch-v4.json"
)


def _observation(
    name: str,
    *,
    eligible: bool = True,
    l2_multicall: bool = False,
    observed_at: str = "",
) -> CapacityPilotObservation:
    return CapacityPilotObservation(
        batch_id=name,
        observed_at=observed_at or f"2026-10-23T00:00:0{name[-1]}+00:00",
        arm="L2" if l2_multicall else "L0",
        status="pass" if eligible else "error",
        llm_calls=2 if l2_multicall else 1,
        subtask_count=1 if l2_multicall else 0,
        retry_event_count=0,
        explicit_retry_telemetry=True,
        rate_limited=not eligible,
        eligible=eligible,
        l2_multicall=l2_multicall,
        reasons=() if eligible else ("Run Record 含 429/RateLimit",),
    )


def test_capacity_gate_requires_two_current_successes_and_one_l2() -> None:
    result = evaluate_capacity_gate(
        [_observation("run-1"), _observation("run-2", l2_multicall=True)]
    )

    assert result.ready is True
    assert result.current_success_streak == 2
    assert result.l2_in_current_streak is True


def test_capacity_gate_resets_streak_after_429() -> None:
    result = evaluate_capacity_gate(
        [
            _observation("run-1", l2_multicall=True),
            _observation("run-2", eligible=False),
            _observation("run-3"),
        ]
    )

    assert result.ready is False
    assert result.current_success_streak == 1
    assert "当前连续有效 pilot 1 < 2" in result.reasons


def test_capacity_gate_rejects_streak_without_l2_multicall() -> None:
    result = evaluate_capacity_gate([_observation("run-1"), _observation("run-2")])

    assert result.ready is False
    assert result.current_success_streak == 2
    assert "当前连续成功段中没有 L2 多调用 pilot" in result.reasons


class _PilotExecutor:
    def __init__(self, *, retry_events: list[dict] | None = None) -> None:
        self._retry_events = retry_events or []

    async def execute(self, spec, trial_index: int):
        arm = str(spec.config["arm"])
        return TrialEvidence(
            status="pass",
            metrics=RunMetrics(llm_calls=2 if arm == "L2" else 1),
            evidence={
                "subtask_count": 1 if arm == "L2" else 0,
                "llm_retry_events": list(self._retry_events),
            },
        )


@pytest.mark.asyncio
async def test_capacity_gate_loads_real_batch_and_run_record_layout(tmp_path) -> None:
    protocol = replace(
        load_protocol(PROTOCOL_PATH), tasks=load_protocol(PROTOCOL_PATH).tasks[:1]
    )
    root = tmp_path / "experiments"
    runner = BatchRunner(ExperimentStore(root), protocol)

    await runner.run(
        phase="pilot",
        task_ids=(protocol.tasks[0].task_id,),
        arm_names=("L0",),
        batch_id="capacity-l0",
        executor_factory=lambda spec: _PilotExecutor(),
    )
    await runner.run(
        phase="pilot",
        task_ids=(protocol.tasks[0].task_id,),
        arm_names=("L2",),
        batch_id="capacity-l2",
        executor_factory=lambda spec: _PilotExecutor(),
    )

    result = load_capacity_gate(
        root,
        batch_ids=("capacity-l0", "capacity-l2"),
        protocol_hash=protocol.config_hash,
    )

    assert result.ready is True
    assert [item.batch_id for item in result.observations] == [
        "capacity-l0",
        "capacity-l2",
    ]
