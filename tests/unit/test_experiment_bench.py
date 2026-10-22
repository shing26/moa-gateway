from __future__ import annotations

import json
from dataclasses import replace

import pytest

from bench.experiment import (
    BenchExperimentAdapter,
    CollaborationExperimentAdapter,
    ExperimentRunner,
    ExperimentSpec,
    ExperimentStore,
    RunMetrics,
    RunRecord,
    TrialEvidence,
    WorkspaceSnapshot,
    compare_run_records,
    fingerprint_workspace,
)


def test_workspace_fingerprint_tracks_content_and_ignores_runtime_state(tmp_path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_text("noise", encoding="utf-8")

    first = fingerprint_workspace(tmp_path)
    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    second = fingerprint_workspace(tmp_path)

    assert first.file_count == 1
    assert first.content_hash != second.content_hash
    assert first.path == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_runner_creates_one_immutable_record_per_trial(tmp_path) -> None:
    class FakeExecutor:
        def __init__(self) -> None:
            self.indices: list[int] = []

        async def execute(self, spec: ExperimentSpec, trial_index: int) -> TrialEvidence:
            self.indices.append(trial_index)
            return TrialEvidence(
                status="ok",
                plan=({"index": 0, "agent": "coder", "depends_on": []},),
                node_path=("plan", "deliver"),
                per_agent_outputs={"coder": f"trial-{trial_index}"},
                final_text=f"trial-{trial_index}",
                metrics=RunMetrics(
                    llm_calls=2,
                    prompt_tokens=10,
                    completion_tokens=5,
                    llm_latency_ms=12.5,
                ),
                evidence={"critic": {"decision": "approve"}, "guard_action": "allow"},
                workspace=WorkspaceSnapshot(
                    path=str(tmp_path),
                    before_hash="before",
                    after_hash="after",
                    files_before=1,
                    files_after=1,
                ),
            )

    spec = ExperimentSpec(
        experiment_id="exp-unit",
        task="做一件可验证的事",
        adapter="fake",
        trials=2,
        workspace_ref=str(tmp_path),
        config={"max_rounds": 2},
        model={"model": "fake-model", "temperature": 0.0},
        verify_command=("python", "-m", "pytest", "-q"),
    )
    executor = FakeExecutor()
    outcome = await ExperimentRunner(ExperimentStore(tmp_path / "records")).run(
        spec, executor
    )

    assert executor.indices == [0, 1]
    assert len(outcome.records) == 2
    assert outcome.manifest.trial_count == 2
    assert outcome.manifest.status_counts == {"ok": 2}
    assert len({record.trial_id for record in outcome.records}) == 2
    assert all(record.config_hash == outcome.manifest.config_hash for record in outcome.records)
    assert all(record.path and record.path.exists() for record in outcome.records)

    payload = json.loads(outcome.records[0].path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["experiment_id"] == "exp-unit"
    assert payload["metrics"]["prompt_tokens"] == 10
    assert payload["model"]["model"] == "fake-model"
    assert payload["config"]["max_rounds"] == 2
    assert payload["verify_command"] == ["python", "-m", "pytest", "-q"]
    assert payload["workspace"]["snapshot_id"]


@pytest.mark.asyncio
async def test_trial_exception_is_recorded_and_does_not_hide_later_trials(tmp_path) -> None:
    class FlakyExecutor:
        async def execute(self, spec: ExperimentSpec, trial_index: int) -> TrialEvidence:
            if trial_index == 0:
                raise RuntimeError("provider unavailable")
            return TrialEvidence(status="ok", final_text="recovered")

    outcome = await ExperimentRunner(ExperimentStore(tmp_path / "records")).run(
        ExperimentSpec(
            experiment_id="exp-flaky",
            task="容忍单次失败",
            adapter="fake",
            trials=2,
        ),
        FlakyExecutor(),
    )

    assert [record.status for record in outcome.records] == ["error", "ok"]
    assert "provider unavailable" in outcome.records[0].error
    assert outcome.manifest.status_counts == {"error": 1, "ok": 1}


@pytest.mark.asyncio
async def test_runner_refuses_to_overwrite_existing_experiment(tmp_path) -> None:
    class FakeExecutor:
        async def execute(self, spec: ExperimentSpec, trial_index: int) -> TrialEvidence:
            return TrialEvidence(status="ok")

    store = ExperimentStore(tmp_path / "records")
    spec = ExperimentSpec(
        experiment_id="exp-once",
        task="只跑一次",
        adapter="fake",
    )
    await ExperimentRunner(store).run(spec, FakeExecutor())

    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        await ExperimentRunner(store).run(spec, FakeExecutor())


def _record(*, plan: tuple[dict, ...], status: str = "ok") -> RunRecord:
    return RunRecord(
        schema_version=1,
        experiment_id="exp",
        trial_id="trial",
        trial_index=0,
        created_at="2026-10-22T00:00:00+00:00",
        duration_ms=10.0,
        task="task",
        task_hash="task-hash",
        config_hash="config-hash",
        adapter="collaboration",
        status=status,
        plan=plan,
        node_path=("plan", "dispatch", "critic:1", "deliver"),
        per_agent_outputs={"coder": "different natural text is allowed"},
        final_text="different text",
        metrics=RunMetrics(prompt_tokens=10),
        evidence={
            "critic": {"decision": "approve", "targets": []},
            "guard_action": "allow",
            "hitl_kind": "",
            "tool_calls": 1,
        },
        workspace=WorkspaceSnapshot(before_hash="same-workspace"),
        error="",
    )


def test_compare_ignores_natural_text_but_reports_behavior_drift() -> None:
    plan_a = ({"index": 0, "agent": "coder", "depends_on": []},)
    baseline = _record(plan=plan_a)
    same_behavior = _record(plan=plan_a)
    changed_plan = _record(
        plan=(),
        status="error",
    )

    assert compare_run_records(baseline, same_behavior) == []

    diffs = compare_run_records(baseline, changed_plan)
    assert "status" in diffs
    assert "plan" in diffs

    changed_config = replace(
        same_behavior,
        config_hash="different-config",
        workspace=WorkspaceSnapshot(before_hash="different-workspace"),
    )
    config_diffs = compare_run_records(baseline, changed_config)
    assert "config_hash" in config_diffs
    assert "workspace.before_hash" in config_diffs


@pytest.mark.asyncio
async def test_collaboration_adapter_maps_cumulative_usage(tmp_path) -> None:
    from app.orchestration.collaboration import CollabResult, SubtaskAssignment

    class FakeOrchestrator:
        async def run(self, **kwargs):
            assert kwargs["workspace_path"] == str(tmp_path)
            assert kwargs["verify_command"] == ("python", "-m", "pytest")
            return CollabResult(
                trace_id=kwargs["trace_id"],
                task=kwargs["task"],
                plan=(SubtaskAssignment(0, "读取", "coder", ()),),
                rounds=1,
                per_agent_outputs={"coder": "读取完成"},
                critique_history=(),
                final_text="协作完成",
                status="ok",
                need_human_review=False,
                cost_usd=0.01,
                llm_latency_ms=50.0,
                tool_calls=1,
                node_path=("plan", "dispatch", "critic:1", "deliver"),
                guard_action="allow",
            )

    class FakeUsage:
        usage_totals = {
            "llm_calls": 3,
            "prompt_tokens": 30,
            "completion_tokens": 12,
            "cost_usd": 0.01,
            "llm_latency_ms": 50.0,
            "prompt_hashes": ["prompt-a", "prompt-b", "prompt-c"],
        }

    adapter = CollaborationExperimentAdapter(
        orchestrator=FakeOrchestrator(),
        usage_source=FakeUsage(),
    )
    evidence = await adapter.execute(
        ExperimentSpec(
            experiment_id="exp-collab",
            task="读取文件",
            adapter="collaboration",
            workspace_ref=str(tmp_path),
            verify_command=("python", "-m", "pytest"),
        ),
        0,
    )

    assert evidence.status == "ok"
    assert evidence.metrics.llm_calls == 3
    assert evidence.metrics.prompt_tokens == 30
    assert evidence.metrics.tool_calls == 1
    assert evidence.prompt_hashes == ("prompt-a", "prompt-b", "prompt-c")
    assert evidence.plan[0]["agent"] == "coder"
    assert evidence.evidence["guard_action"] == "allow"
    assert evidence.workspace.before_hash


@pytest.mark.asyncio
async def test_bench_adapter_maps_harness_verdict_and_steps(tmp_path) -> None:
    from bench.agent.types import AgentRun, RunResult, Step, Usage

    result = RunResult(
        task_id="task-1",
        arm="L3",
        base_commit="abc123",
        agent_run=AgentRun(
            status="completed",
            steps=(
                Step(
                    index=0,
                    action="call_tool",
                    tool="run_tests",
                    outcome="failed",
                    observation="failed",
                ),
                Step(index=1, action="finish", observation="done"),
            ),
            usage=Usage(
                llm_calls=4,
                prompt_tokens=40,
                completion_tokens=20,
                cost_usd=0.02,
                llm_latency_ms=80.0,
                prompt_hashes=("prompt-a", "prompt-b"),
            ),
            model="model-x",
            temperature=0.0,
            review_rounds=1,
            subtask_count=2,
        ),
        outcome="pass",
        patch="diff --git a/x b/x\n",
        fail_first_exit_code=1,
        verdict_exit_code=0,
        verdict_output="1 passed",
        wall_clock_s=1.2,
    )

    async def fake_run(spec: ExperimentSpec, trial_index: int):
        return result, tmp_path

    evidence = await BenchExperimentAdapter(fake_run).execute(
        ExperimentSpec(task="修复", adapter="bench:L3", trials=1),
        0,
    )

    assert evidence.status == "pass"
    assert evidence.metrics.llm_calls == 4
    assert evidence.metrics.tool_calls == 1
    assert evidence.metrics.tool_errors == 1
    assert evidence.prompt_hashes == ("prompt-a", "prompt-b")
    assert evidence.evidence["arm"] == "L3"
    assert evidence.evidence["patch_touched_tests"] is False
