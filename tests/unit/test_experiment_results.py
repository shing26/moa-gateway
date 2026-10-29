from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from app.main import app
from app.services.experiment_results import ExperimentResultService
from bench.experiment import (
    BatchRunner,
    ExperimentStore,
    RunMetrics,
    TrialEvidence,
    load_protocol,
)
from tests.support import app_client


class ResultExecutor:
    async def execute(self, spec, trial_index: int):
        return TrialEvidence(
            status="pass",
            plan=(
                {
                    "index": 0,
                    "instruction": "修复失败断言",
                    "depends_on": [],
                },
            ),
            node_path=("read_file", "write_file", "run_tests"),
            final_text="verified",
            metrics=RunMetrics(
                llm_calls=3,
                prompt_tokens=120,
                completion_tokens=30,
                cost_usd=0.02,
                tool_calls=2,
            ),
            prompt_hashes=("prompt-a", "prompt-b", "prompt-c"),
            evidence={
                "steps": [
                    {
                        "index": 0,
                        "action": "call_tool",
                        "tool": "read_file",
                        "outcome": "ok",
                        "elapsed_ms": 1.5,
                    },
                    {
                        "index": 1,
                        "action": "call_tool",
                        "tool": "run_tests",
                        "outcome": "ok",
                        "elapsed_ms": 20.0,
                    },
                ],
                "budget_tier": "B1",
            },
        )


def _build_results(tmp_path):
    protocol = replace(
        load_protocol(),
        tasks=load_protocol().tasks[:1],
        repeats=replace(load_protocol().repeats, baseline=1),
    )
    root = tmp_path / "experiments"
    asyncio.run(
        BatchRunner(ExperimentStore(root), protocol).run(
            phase="baseline",
            arm_names=("L0",),
            batch_id="batch-ui",
            executor_factory=lambda spec: ResultExecutor(),
        )
    )
    return root


class CancelSecondTrialExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, spec, trial_index: int):
        self.calls += 1
        if self.calls == 2:
            raise asyncio.CancelledError
        return TrialEvidence(status="pass")


def _build_partial_results(tmp_path):
    base = load_protocol()
    protocol = replace(
        base,
        tasks=base.tasks[:1],
        repeats=replace(base.repeats, formal=2),
    )
    root = tmp_path / "experiments"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            BatchRunner(ExperimentStore(root), protocol).run(
                phase="formal",
                arm_names=("L0",),
                batch_id="batch-partial",
                executor_factory=lambda spec: CancelSecondTrialExecutor(),
            )
        )
    return root


def test_experiment_result_service_reads_batches_and_guards_paths(tmp_path) -> None:
    root = _build_results(tmp_path)
    service = ExperimentResultService(root)

    batches = service.list_batches()
    assert len(batches) == 1
    assert batches[0]["batch_id"] == "batch-ui"
    assert batches[0]["completed_runs"] == 1
    assert batches[0]["expected_runs"] == 1

    batch = service.batch("batch-ui")
    assert batch["manifest"]["status"] == "completed"
    assert len(batch["cells"]) == 1
    cell = batch["cells"][0]
    assert len(cell["runs"]) == 1
    experiment_id = cell["experiment_id"]
    assert cell["runs"][0]["trial_index"] == 0

    record = service.run("batch-ui", experiment_id, 0)
    assert record["status"] == "pass"
    assert record["plan"][0]["instruction"] == "修复失败断言"
    assert record["_relative_path"].endswith("trial-000.json")

    with pytest.raises(ValueError, match="batch_id"):
        service.batch("../escape")
    with pytest.raises(FileNotFoundError, match="不属于批次"):
        service.run("batch-ui", "exp-not-in-batch", 0)


def test_unfinished_batch_uses_manifest_progress_before_report_exists(
    tmp_path,
    monkeypatch,
) -> None:
    root = _build_partial_results(tmp_path)
    service = ExperimentResultService(root)

    row = service.list_batches()[0]
    assert row["status"] == "paused"
    assert row["completed_cells"] == 0
    assert row["completed_runs"] == 1
    assert row["expected_runs"] == 2
    assert row["missing_runs"] == 1

    batch = service.batch("batch-partial")
    assert batch["report"]["completed_runs"] == 1
    assert batch["report"]["expected_runs"] == 2
    assert batch["report"]["missing_runs"] == 1
    assert batch["report"]["analysis_authorized"] is False
    assert batch["report"]["statistical_claims"] is False

    monkeypatch.setenv("BENCH_EXPERIMENTS_DIR", str(root))
    with app_client(app) as client:
        detail = client.get("/dashboard/experiments/batch-partial")
        assert detail.status_code == 200
        assert 'data-label="样本量" class="mono">1 / 2</td>' in detail.text


def test_unfinished_batch_counts_trial_files_when_manifest_lags(tmp_path) -> None:
    root = _build_partial_results(tmp_path)
    manifest_path = root / "_batches" / "batch-partial" / "batch.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "running"
    manifest["cells"][0]["status"] = "running"
    manifest["cells"][0]["trial_count"] = 0
    manifest["cells"][0]["status_counts"] = {}
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    service = ExperimentResultService(root)
    row = service.list_batches()[0]
    assert row["completed_runs"] == 1
    assert row["missing_runs"] == 1

    batch = service.batch("batch-partial")
    assert batch["report"]["completed_runs"] == 1
    assert batch["cells"][0]["trial_count"] == 1
    assert batch["cells"][0]["status_counts"] == {"pass": 1}


def test_experiment_dashboard_is_read_only_and_links_run_records(
    tmp_path, monkeypatch
) -> None:
    root = _build_results(tmp_path)
    monkeypatch.setenv("BENCH_EXPERIMENTS_DIR", str(root))
    batch = ExperimentResultService(root).batch("batch-ui")
    experiment_id = batch["cells"][0]["experiment_id"]

    with app_client(app) as client:
        api = client.get("/dashboard/api/experiments")
        assert api.status_code == 200
        assert api.json()["batches"][0]["batch_id"] == "batch-ui"

        page = client.get("/dashboard/experiments")
        assert page.status_code == 200
        assert "batch-ui" in page.text

        detail = client.get("/dashboard/experiments/batch-ui")
        assert detail.status_code == 200
        assert "0896629" in detail.text
        assert f"/runs/{experiment_id}/0" in detail.text
        assert 'class="data-table responsive-stack"' in detail.text
        assert 'class="table-wrap table-wrap-responsive"' in detail.text
        assert 'data-label="任务"' in detail.text
        assert 'data-label="记录"' in detail.text
        assert "统计比较未授权" in detail.text
        assert "并发上限" in detail.text
        assert "平均成本" in detail.text
        assert "$0.0200" in detail.text
        assert "样本量" in detail.text

        run = client.get(
            f"/dashboard/experiments/batch-ui/runs/{experiment_id}/0"
        )
        assert run.status_code == 200
        assert "完整 Run Record" in run.text
        assert "run_tests" in run.text
        assert "trial-000.json" in run.text

        run_api = client.get(
            f"/dashboard/api/experiments/batch-ui/runs/{experiment_id}/0"
        )
        assert run_api.status_code == 200
        assert run_api.json()["metrics"]["total_tokens"] == 150

        assert client.post("/dashboard/experiments").status_code == 405


def test_experiment_dashboard_missing_batch_returns_404(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("BENCH_EXPERIMENTS_DIR", str(tmp_path / "missing"))
    with app_client(app) as client:
        assert client.get("/dashboard/experiments").status_code == 200
        assert client.get("/dashboard/experiments/nope").status_code == 404
