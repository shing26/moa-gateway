from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from bench import run_batch as batch_cli
from bench.experiment import (
    BatchRunner,
    BatchStore,
    ExperimentRunner,
    ExperimentStore,
    RunMetrics,
    TrialEvidence,
    build_batch_report,
    load_protocol,
)


class FakeExecutor:
    def __init__(
        self,
        outcomes: dict[tuple[str, str, int], str] | None = None,
        *,
        cancel_once: tuple[str, str, int] | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.cancel_once = cancel_once
        self.calls: list[tuple[str, str, int]] = []

    async def execute(self, spec, trial_index: int):
        key = (
            str(spec.config["task_id"]),
            str(spec.config["arm"]),
            trial_index,
        )
        self.calls.append(key)
        if self.cancel_once == key:
            self.cancel_once = None
            raise asyncio.CancelledError
        status = self.outcomes.get(key, "pass")
        return TrialEvidence(
            status=status,
            final_text=f"{key[0]}:{key[1]}:{trial_index}",
            metrics=RunMetrics(
                llm_calls=1,
                prompt_tokens=100 + trial_index,
                completion_tokens=20,
            ),
            evidence={"synthetic": True},
        )


def _two_task_protocol():
    protocol = load_protocol()
    return replace(protocol, tasks=protocol.tasks[:2])


@pytest.mark.asyncio
async def test_batch_runs_the_matrix_and_resume_skips_completed_evidence(
    tmp_path,
) -> None:
    protocol = _two_task_protocol()
    experiments = ExperimentStore(tmp_path / "experiments")
    runner = BatchRunner(experiments, protocol)
    first_executor = FakeExecutor()

    first = await runner.run(
        phase="baseline",
        batch_id="batch-unit",
        executor_factory=lambda spec: first_executor,
    )

    assert first.manifest.status == "completed"
    assert len(first.manifest.cells) == 8
    assert first.manifest.resume_cursor == ""
    assert len(first_executor.calls) == 8
    assert all(cell.status == "completed" for cell in first.manifest.cells)
    assert all(cell.trial_count == 1 for cell in first.manifest.cells)

    second_executor = FakeExecutor()

    def should_not_build(spec):
        raise AssertionError(f"已完成单元不应再装配执行器: {spec.experiment_id}")

    second = await runner.run(
        phase="baseline",
        batch_id="batch-unit",
        executor_factory=should_not_build,
    )

    assert second.manifest.status == "completed"
    assert second.manifest.cells == first.manifest.cells
    assert second_executor.calls == []
    assert BatchStore(tmp_path / "experiments").manifest_path("batch-unit").exists()


@pytest.mark.asyncio
async def test_interrupted_trial_resumes_without_overwriting_prior_record(
    tmp_path,
) -> None:
    protocol = replace(
        load_protocol(),
        tasks=load_protocol().tasks[:1],
        repeats=replace(load_protocol().repeats, formal=2),
    )
    experiment_store = ExperimentStore(tmp_path / "experiments")
    runner = BatchRunner(experiment_store, protocol)
    first_executor = FakeExecutor(
        cancel_once=("0896629", "L0", 1),
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run(
            phase="formal",
            arm_names=("L0",),
            batch_id="batch-resume",
            executor_factory=lambda spec: first_executor,
        )

    interrupted = BatchStore(tmp_path / "experiments").load("batch-resume")
    cell = interrupted.cells[0]
    experiment_id = cell.experiment_id
    first_record_path = experiment_store.trial_path(experiment_id, 0)
    first_record_bytes = first_record_path.read_bytes()
    assert cell.trial_count == 1
    assert interrupted.status == "paused"

    second_executor = FakeExecutor()
    resumed = await runner.run(
        phase="formal",
        arm_names=("L0",),
        batch_id="batch-resume",
        executor_factory=lambda spec: second_executor,
    )

    assert second_executor.calls == [("0896629", "L0", 1)]
    assert resumed.manifest.status == "completed"
    assert resumed.manifest.cells[0].trial_count == 2
    assert first_record_path.read_bytes() == first_record_bytes
    assert experiment_store.load_manifest(experiment_id).trial_count == 2


@pytest.mark.asyncio
async def test_cell_error_is_retained_and_batch_continues(tmp_path) -> None:
    protocol = replace(load_protocol(), tasks=load_protocol().tasks[:1])
    runner = BatchRunner(ExperimentStore(tmp_path / "experiments"), protocol)
    executor = FakeExecutor()

    def factory(spec):
        if spec.config["arm"] == "L1":
            raise RuntimeError("executor wiring failed")
        return executor

    outcome = await runner.run(
        phase="baseline",
        arm_names=("L0", "L1"),
        batch_id="batch-cell-error",
        executor_factory=factory,
    )

    assert outcome.manifest.status == "completed_with_errors"
    assert [cell.status for cell in outcome.manifest.cells] == ["completed", "error"]
    assert "executor wiring failed" in outcome.manifest.cells[1].error
    assert outcome.manifest.resume_cursor == outcome.manifest.cells[1].cell_id
    assert executor.calls == [("0896629", "L0", 0)]


@pytest.mark.asyncio
async def test_existing_batch_rejects_a_different_protocol_hash(tmp_path) -> None:
    protocol = replace(load_protocol(), tasks=load_protocol().tasks[:1])
    runner = BatchRunner(ExperimentStore(tmp_path / "experiments"), protocol)
    executor = FakeExecutor()

    await runner.run(
        phase="baseline",
        arm_names=("L0",),
        batch_id="batch-frozen",
        executor_factory=lambda spec: executor,
    )

    changed = replace(protocol, version="999")
    changed_runner = BatchRunner(ExperimentStore(tmp_path / "experiments"), changed)
    with pytest.raises(ValueError, match="protocol_hash"):
        await changed_runner.run(
            phase="baseline",
            arm_names=("L0",),
            batch_id="batch-frozen",
            executor_factory=lambda spec: executor,
        )


@pytest.mark.asyncio
async def test_batch_report_uses_run_records_and_keeps_errors_out_of_pass_rate(
    tmp_path,
) -> None:
    protocol = replace(
        load_protocol(),
        tasks=load_protocol().tasks[:1],
        repeats=replace(load_protocol().repeats, formal=2),
    )
    experiments = ExperimentStore(tmp_path / "experiments")
    runner = BatchRunner(experiments, protocol)
    executor = FakeExecutor(
        {
            ("0896629", "L0", 0): "pass",
            ("0896629", "L0", 1): "error",
        }
    )

    outcome = await runner.run(
        phase="formal",
        arm_names=("L0",),
        batch_id="batch-report",
        executor_factory=lambda spec: executor,
    )
    report = build_batch_report(outcome.manifest, experiments)

    cell = report.dose.cell("0896629", "L0")
    assert cell.runs == 2
    assert cell.valid_runs == 1
    assert cell.pass_rate == 1.0
    assert cell.errors == 1
    assert report.to_dict()["statistical_claims"] is False
    assert report.to_dict()["completed_runs"] == 2
    assert report.dose.arms == ("L0",)


def test_batch_cli_runs_a_full_mock_subset_and_writes_reports(
    tmp_path, capsys
) -> None:
    code = batch_cli.main(
        [
            "batch",
            "--phase",
            "pilot",
            "--tasks",
            "0896629",
            "60c64ed",
            "--arms",
            "L0",
            "L3",
            "--mode",
            "mock",
            "--out",
            str(tmp_path / "experiments"),
            "--batch-id",
            "batch-cli",
        ]
    )

    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "completed"
    assert summary["cell_count"] == 4
    assert summary["completed_runs"] == 4
    batch_dir = BatchStore(tmp_path / "experiments").batch_dir("batch-cli")
    assert (batch_dir / "report.json").exists()
    assert (batch_dir / "report.md").exists()
