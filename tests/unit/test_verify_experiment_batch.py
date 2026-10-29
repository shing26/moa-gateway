from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from bench.experiment import (
    BatchRunner,
    BatchStore,
    ExperimentStore,
    RunMetrics,
    TrialEvidence,
    load_protocol,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "verify_experiment_batch.py"
PROTOCOL = PROJECT_ROOT / "bench/protocols/multi-agent-first-batch-v3.json"


class PassingExecutor:
    async def execute(self, spec, trial_index: int):
        return TrialEvidence(
            status="pass",
            metrics=RunMetrics(
                llm_calls=1,
                prompt_tokens=10,
                completion_tokens=5,
            ),
        )


def _build_batch(root: Path, protocol_path: Path) -> None:
    base = load_protocol(PROTOCOL)
    protocol = replace(
        base,
        tasks=base.tasks[:1],
        repeats=replace(base.repeats, baseline=1),
    )
    protocol_path.write_text(
        json.dumps(protocol.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    asyncio.run(
        BatchRunner(ExperimentStore(root), protocol).run(
            phase="baseline",
            arm_names=("L0", "L3"),
            batch_id="verified-batch",
            executor_factory=lambda spec: PassingExecutor(),
        )
    )


def _run_verifier(
    root: Path, protocol_path: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--batch-id",
            "verified-batch",
            "--protocol",
            str(protocol_path),
            "--out",
            str(root),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_verify_experiment_batch_reconciles_report_records_and_api(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiments"
    protocol_path = tmp_path / "protocol.json"
    _build_batch(root, protocol_path)

    result = _run_verifier(root, protocol_path)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["expected_runs"] == 2
    assert payload["record_count"] == 2
    assert payload["completed_cells"] == 2
    assert len(payload["report_sha256"]) == 64


def test_verify_experiment_batch_rejects_tampered_report(tmp_path: Path) -> None:
    root = tmp_path / "experiments"
    protocol_path = tmp_path / "protocol.json"
    _build_batch(root, protocol_path)
    store = BatchStore(root)
    report_path = store.report_json_path("verified-batch")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["completed_runs"] += 1
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    result = _run_verifier(root, protocol_path)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "report.json 与 Run Record 重算不一致" in payload["error"]
