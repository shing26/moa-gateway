from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from bench import run_batch as batch_cli
from bench.experiment import (
    BatchRunner,
    BatchManifest,
    BatchStore,
    ExperimentRunner,
    ExperimentStore,
    RunMetrics,
    TrialEvidence,
    build_batch_report,
    load_protocol,
)

STATS_PROTOCOL_PATH = (
    Path(__file__).resolve().parents[2]
    / "bench"
    / "protocols"
    / "multi-agent-first-batch-v4.json"
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
            evidence={
                "synthetic": True,
                "budget_tier": spec.config.get("budget_tier", "B1"),
                "baseline_cost_usd": spec.config.get("baseline_cost_usd", 0.0),
                "baseline_tokens": spec.config.get("baseline_tokens", 0),
            },
        )


class ConcurrencyProbeExecutor:
    def __init__(self, *, delay_s: float = 0.02) -> None:
        self.delay_s = delay_s
        self.active = 0
        self.max_active = 0
        self.starts: list[float] = []

    async def execute(self, spec, trial_index: int):
        loop = asyncio.get_running_loop()
        self.starts.append(loop.time())
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay_s)
        finally:
            self.active -= 1
        return TrialEvidence(
            status="pass",
            final_text=f"{spec.config['task_id']}:{spec.config['arm']}",
        )


def _two_task_protocol():
    protocol = load_protocol()
    return replace(protocol, tasks=protocol.tasks[:2])


def test_batch_manifest_v1_migrates_execution_controls_with_safe_defaults() -> None:
    current = BatchManifest(
        schema_version=1,
        batch_id="batch-v1",
        protocol_id="protocol",
        protocol_version="1",
        protocol_hash="hash",
        phase="baseline",
        budget_tier="B1",
        source_batch_id="",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        status="completed",
        resume_cursor="",
        cells=(),
    ).to_dict()
    current.pop("max_concurrency")
    current.pop("rate_limit_per_minute")

    manifest = BatchManifest.from_dict(current)

    assert manifest.max_concurrency == 1
    assert manifest.rate_limit_per_minute == 0


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
async def test_batch_runs_cells_concurrently_up_to_global_limit(tmp_path) -> None:
    protocol = replace(load_protocol(), tasks=load_protocol().tasks[:1])
    executor = ConcurrencyProbeExecutor(delay_s=0.03)

    outcome = await BatchRunner(
        ExperimentStore(tmp_path / "experiments"), protocol
    ).run(
        phase="baseline",
        batch_id="batch-concurrent",
        max_concurrency=2,
        executor_factory=lambda spec: executor,
    )

    assert outcome.manifest.status == "completed"
    assert outcome.manifest.max_concurrency == 2
    assert executor.max_active == 2
    assert len(executor.starts) == 4


@pytest.mark.asyncio
async def test_batch_marks_only_scheduled_cells_running(tmp_path) -> None:
    protocol = replace(
        load_protocol(),
        tasks=load_protocol().tasks[:1],
        repeats=replace(load_protocol().repeats, baseline=1),
    )
    store = ExperimentStore(tmp_path / "experiments")
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingExecutor:
        async def execute(self, spec, trial_index: int):
            started.set()
            await release.wait()
            return TrialEvidence(status="pass")

    task = asyncio.create_task(
        BatchRunner(store, protocol).run(
            phase="baseline",
            arm_names=("L0", "L1"),
            batch_id="batch-active-status",
            max_concurrency=1,
            executor_factory=lambda spec: BlockingExecutor(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    active = BatchStore(store.root).load("batch-active-status")
    assert [cell.status for cell in active.cells] == ["running", "pending"]
    assert sum(cell.status == "running" for cell in active.cells) == 1

    release.set()
    outcome = await asyncio.wait_for(task, timeout=2)
    assert outcome.manifest.status == "completed"
    assert [cell.status for cell in outcome.manifest.cells] == [
        "completed",
        "completed",
    ]


@pytest.mark.asyncio
async def test_batch_persists_trial_progress_before_cell_completes(tmp_path) -> None:
    protocol = replace(
        load_protocol(),
        tasks=load_protocol().tasks[:1],
        repeats=replace(load_protocol().repeats, baseline=2),
    )
    store = ExperimentStore(tmp_path / "experiments")
    second_started = asyncio.Event()
    release = asyncio.Event()

    class BlockingSecondTrial:
        async def execute(self, spec, trial_index: int):
            if trial_index == 1:
                second_started.set()
                await release.wait()
            return TrialEvidence(status="pass")

    task = asyncio.create_task(
        BatchRunner(store, protocol).run(
            phase="baseline",
            arm_names=("L0",),
            batch_id="batch-trial-progress",
            max_concurrency=1,
            executor_factory=lambda spec: BlockingSecondTrial(),
        )
    )
    await asyncio.wait_for(second_started.wait(), timeout=1)

    active = BatchStore(store.root).load("batch-trial-progress")
    assert active.cells[0].status == "running"
    assert active.cells[0].trial_count == 1
    assert active.cells[0].status_counts == {"pass": 1}

    release.set()
    outcome = await asyncio.wait_for(task, timeout=2)
    assert outcome.manifest.cells[0].trial_count == 2


@pytest.mark.asyncio
async def test_batch_enforces_trial_start_rate_limit(tmp_path) -> None:
    protocol = replace(
        load_protocol(),
        tasks=load_protocol().tasks[:1],
        repeats=replace(load_protocol().repeats, baseline=1),
    )
    executor = ConcurrencyProbeExecutor(delay_s=0.0)

    await BatchRunner(ExperimentStore(tmp_path / "experiments"), protocol).run(
        phase="baseline",
        arm_names=("L0", "L1", "L2"),
        batch_id="batch-paced",
        max_concurrency=3,
        rate_limit_per_minute=300,
        executor_factory=lambda spec: executor,
    )

    gaps = [
        later - earlier
        for earlier, later in zip(executor.starts, executor.starts[1:])
    ]
    assert len(gaps) == 2
    assert all(gap >= 0.15 for gap in gaps)


@pytest.mark.asyncio
async def test_resume_rejects_changed_concurrency_contract(tmp_path) -> None:
    protocol = replace(load_protocol(), tasks=load_protocol().tasks[:1])
    runner = BatchRunner(ExperimentStore(tmp_path / "experiments"), protocol)
    await runner.run(
        phase="baseline",
        arm_names=("L0",),
        batch_id="batch-limit-frozen",
        max_concurrency=1,
        executor_factory=lambda spec: FakeExecutor(),
    )

    with pytest.raises(ValueError, match="并发上限"):
        await runner.run(
            phase="baseline",
            arm_names=("L0",),
            batch_id="batch-limit-frozen",
            max_concurrency=2,
            executor_factory=lambda spec: FakeExecutor(),
        )


@pytest.mark.asyncio
async def test_concurrent_and_serial_runs_keep_the_same_evidence_contract(
    tmp_path,
) -> None:
    protocol = replace(load_protocol(), tasks=load_protocol().tasks[:1])
    serial_store = ExperimentStore(tmp_path / "serial")
    concurrent_store = ExperimentStore(tmp_path / "concurrent")

    serial = await BatchRunner(serial_store, protocol).run(
        phase="baseline",
        arm_names=("L0",),
        batch_id="batch-contract",
        max_concurrency=1,
        executor_factory=lambda spec: FakeExecutor(),
    )
    concurrent = await BatchRunner(concurrent_store, protocol).run(
        phase="baseline",
        arm_names=("L0",),
        batch_id="batch-contract",
        max_concurrency=2,
        executor_factory=lambda spec: FakeExecutor(),
    )

    serial_record = serial_store.load_experiment_records(
        serial.manifest.cells[0].experiment_id
    )[0]
    concurrent_record = concurrent_store.load_experiment_records(
        concurrent.manifest.cells[0].experiment_id
    )[0]
    assert serial_record.config_hash == concurrent_record.config_hash
    assert serial_record.status == concurrent_record.status
    assert serial_record.evidence == concurrent_record.evidence
    assert serial_record.metrics == concurrent_record.metrics


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


@pytest.mark.asyncio
async def test_batch_report_keeps_e3_origin_when_the_only_run_is_an_error(
    tmp_path,
) -> None:
    protocol = load_protocol(STATS_PROTOCOL_PATH)
    task_id = "e3-c2-keyword-normalization"
    experiments = ExperimentStore(tmp_path / "experiments")
    outcome = await BatchRunner(experiments, protocol).run(
        phase="pilot",
        task_ids=(task_id,),
        arm_names=("L0",),
        batch_id="e3-error-origin",
        executor_factory=lambda spec: FakeExecutor(
            {(task_id, "L0", 0): "error"}
        ),
    )

    report = build_batch_report(outcome.manifest, experiments, protocol)
    assert report.paired is not None
    task = next(item for item in report.paired.tasks if item.task_id == task_id)
    assert task.task_origin == "E3"
    assert task.baseline_valid_runs == 0


@pytest.mark.asyncio
async def test_b3_only_reruns_fail_and_budget_exhausted_cells_with_baseline(
    tmp_path,
) -> None:
    protocol = replace(
        load_protocol(),
        tasks=load_protocol().tasks[:1],
        repeats=replace(load_protocol().repeats, formal=2),
    )
    experiments = ExperimentStore(tmp_path / "experiments")
    runner = BatchRunner(experiments, protocol)
    source_executor = FakeExecutor(
        {
            ("0896629", "L0", 0): "pass",
            ("0896629", "L0", 1): "pass",
            ("0896629", "L1", 0): "fail",
            ("0896629", "L1", 1): "pass",
            ("0896629", "L2", 0): "error",
            ("0896629", "L2", 1): "error",
            ("0896629", "L3", 0): "budget_exhausted",
            ("0896629", "L3", 1): "fail",
        }
    )

    source = await runner.run(
        phase="formal",
        batch_id="source-b1",
        executor_factory=lambda spec: source_executor,
    )
    assert source.manifest.budget_tier == "B1"
    assert len(source_executor.calls) == 10
    assert source_executor.calls.count(("0896629", "L2", 0)) == 2
    assert source_executor.calls.count(("0896629", "L2", 1)) == 2

    b3_executor = FakeExecutor()
    outcome = await runner.run_b3(
        source_batch_id="source-b1",
        phase="formal",
        batch_id="rerun-b3",
        executor_factory=lambda spec: b3_executor,
    )

    assert outcome.manifest.budget_tier == "B3"
    assert outcome.manifest.source_batch_id == "source-b1"
    assert [cell.cell_id for cell in outcome.manifest.cells] == [
        "0896629__L1",
        "0896629__L3",
    ]
    assert [cell.expected_trials for cell in outcome.manifest.cells] == [2, 2]
    assert b3_executor.calls == [
        ("0896629", "L1", 0),
        ("0896629", "L1", 1),
        ("0896629", "L3", 0),
        ("0896629", "L3", 1),
    ]

    l1_spec = protocol.experiment_spec(
        "0896629",
        "L1",
        phase="formal",
        batch_id="rerun-b3",
        budget_tier="B3",
        source_batch_id="source-b1",
        baseline_cost_usd=0.0,
        baseline_tokens=241,
    )
    manifest = experiments.load_manifest(l1_spec.experiment_id)
    assert manifest.spec.config["budget_tier"] == "B3"
    assert manifest.spec.config["baseline_tokens"] == 241
    assert manifest.spec.config["budget"] == protocol.budget.b3.to_dict()

    report = build_batch_report(outcome.manifest, experiments, protocol)
    assert report.dose.tier == "B3"
    assert report.to_dict()["budget_tier"] == "B3"
    assert report.to_dict()["source_batch_id"] == "source-b1"
    assert report.dose.total_runs == 4


@pytest.mark.asyncio
async def test_b3_refuses_to_run_when_source_has_no_eligible_cells(tmp_path) -> None:
    protocol = replace(load_protocol(), tasks=load_protocol().tasks[:1])
    runner = BatchRunner(ExperimentStore(tmp_path / "experiments"), protocol)
    await runner.run(
        phase="formal",
        arm_names=("L0",),
        batch_id="source-all-pass",
        executor_factory=lambda spec: FakeExecutor(),
    )

    with pytest.raises(ValueError, match="没有 B3 候选"):
        await runner.run_b3(
            source_batch_id="source-all-pass",
            phase="formal",
            batch_id="rerun-b3",
            executor_factory=lambda spec: FakeExecutor(),
        )


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
            "--max-concurrency",
            "2",
        ]
    )

    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "completed"
    assert summary["max_concurrency"] == 2
    assert summary["cell_count"] == 4
    assert summary["completed_runs"] == 4
    batch_dir = BatchStore(tmp_path / "experiments").batch_dir("batch-cli")
    assert (batch_dir / "report.json").exists()
    assert (batch_dir / "report.md").exists()


def test_batch_cli_rejects_live_models_in_ci(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("BENCH_ALLOW_LIVE_IN_CI", raising=False)

    code = batch_cli.main(
        [
            "batch",
            "--phase",
            "pilot",
            "--tasks",
            "0896629",
            "--arms",
            "L0",
            "--mode",
            "live",
            "--out",
            str(tmp_path / "experiments"),
            "--batch-id",
            "must-not-run",
        ]
    )

    message = json.loads(capsys.readouterr().out)
    assert code == 2
    assert message["error"] == "ValueError"
    assert "拒绝执行 live" in message["message"]
    assert not (tmp_path / "experiments" / "_batches" / "must-not-run").exists()


def test_batch_ci_guard_can_be_explicitly_overridden(monkeypatch) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("BENCH_ALLOW_LIVE_IN_CI", "1")

    batch_cli._guard_live_mode("live")


def test_live_retry_uses_a_fresh_attempt_workspace(tmp_path) -> None:
    first = batch_cli._trial_attempt_workdir(
        tmp_path,
        batch_id="batch-live",
        experiment_id="exp-live",
        trial_index=0,
        task_id="task-1",
    )
    first_workspace = first / "task-1"
    first_workspace.mkdir(parents=True)
    (first_workspace / "partial.txt").write_text("failed attempt", encoding="utf-8")

    second = batch_cli._trial_attempt_workdir(
        tmp_path,
        batch_id="batch-live",
        experiment_id="exp-live",
        trial_index=0,
        task_id="task-1",
    )

    assert first.name == "attempt-000"
    assert second.name == "attempt-001"
    assert not (second / "task-1").exists()


def test_batch_cli_b3_selects_only_failed_cells_and_records_source(
    tmp_path, capsys
) -> None:
    protocol = load_protocol()
    experiments = ExperimentStore(tmp_path / "experiments")
    source_executor = FakeExecutor({("0896629", "L1", 0): "fail"})
    asyncio.run(
        BatchRunner(experiments, protocol).run(
            phase="formal",
            batch_id="source-cli-b1",
            executor_factory=lambda spec: source_executor,
        )
    )
    capsys.readouterr()

    code = batch_cli.main(
        [
            "b3",
            "--source-batch",
            "source-cli-b1",
            "--phase",
            "formal",
            "--mode",
            "mock",
            "--out",
            str(tmp_path / "experiments"),
            "--batch-id",
            "rerun-cli-b3",
        ]
    )

    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["budget_tier"] == "B3"
    assert summary["source_batch_id"] == "source-cli-b1"
    assert summary["cell_count"] == 1
    assert summary["cells"][0]["cell_id"] == "0896629__L1"
    assert summary["completed_runs"] == 3


@pytest.mark.asyncio
async def test_preregistered_batch_report_authorizes_only_complete_paired_data(
    tmp_path,
) -> None:
    protocol = load_protocol(STATS_PROTOCOL_PATH)
    outcomes = {}
    for task in protocol.tasks:
        for repeat in range(protocol.repeats.formal):
            outcomes[(task.task_id, "L0", repeat)] = "fail"
            outcomes[(task.task_id, "L3", repeat)] = "pass"

    outcome = await BatchRunner(
        ExperimentStore(tmp_path / "experiments"), protocol
    ).run(
        phase="formal",
        arm_names=("L0", "L3"),
        batch_id="preregistered-batch",
        executor_factory=lambda spec: FakeExecutor(outcomes),
    )

    assert outcome.report.paired is not None
    assert outcome.report.paired.authorized is True
    assert outcome.report.paired.decision == "candidate_better"
    payload = outcome.report.to_dict()
    assert payload["analysis_authorized"] is True
    assert payload["statistical_claims"] is True
    assert payload["statistics"]["planned_task_count"] == 8
    assert payload["statistics"]["confidence_interval"] == [1.0, 1.0]
    assert payload["model_matrix"][0]["cost_signal"] == "unavailable_or_zero"
    assert "分析门禁：**已按预注册门禁授权**" in outcome.report.render_markdown()


@pytest.mark.asyncio
async def test_preregistered_zero_difference_authorizes_analysis_not_claims(
    tmp_path,
) -> None:
    protocol = load_protocol(STATS_PROTOCOL_PATH)
    outcomes = {}
    for task in protocol.tasks:
        for repeat in range(protocol.repeats.formal):
            outcomes[(task.task_id, "L0", repeat)] = "fail"
            outcomes[(task.task_id, "L3", repeat)] = "fail"

    outcome = await BatchRunner(
        ExperimentStore(tmp_path / "experiments"), protocol
    ).run(
        phase="formal",
        arm_names=("L0", "L3"),
        batch_id="preregistered-zero-difference",
        executor_factory=lambda spec: FakeExecutor(outcomes),
    )

    assert outcome.report.paired is not None
    assert outcome.report.paired.authorized is True
    assert outcome.report.paired.decision == "no_significant_difference"
    payload = outcome.report.to_dict()
    assert payload["analysis_authorized"] is True
    assert payload["statistical_claims"] is False
    markdown = outcome.report.render_markdown()
    assert "分析门禁：**已按预注册门禁授权**" in markdown
    assert "显著差异声明：**不授权**" in markdown
