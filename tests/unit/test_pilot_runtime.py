from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from bench.experiment import (
    BatchRunner,
    BatchStore,
    ExperimentStore,
    RunMetrics,
    TrialEvidence,
    load_pilot_runtime_gate,
    load_protocol,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "verify_pilot_runtime.py"
PROTOCOL = PROJECT_ROOT / "bench/protocols/multi-agent-first-batch-v6-local.json"


class PilotExecutor:
    def __init__(self, status: str = "pass") -> None:
        self.status = status

    async def execute(self, spec, trial_index: int):
        return TrialEvidence(
            status=self.status,
            node_path=("step:0:run_tests",),
            final_text="pilot",
            metrics=RunMetrics(
                llm_calls=1,
                prompt_tokens=10,
                completion_tokens=5,
                tool_calls=1,
            ),
            evidence={
                "steps": [{"action": "call_tool", "tool": "run_tests"}],
            },
            error="executor failed" if self.status == "error" else "",
        )


def _pilot_protocol(path: Path):
    base = load_protocol(PROTOCOL)
    protocol = replace(
        base,
        repeats=replace(base.repeats, pilot=1),
    )
    path.write_text(
        json.dumps(protocol.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return protocol


def _build_pilot(root: Path, protocol_path: Path, *, status: str = "pass"):
    protocol = _pilot_protocol(protocol_path)
    asyncio.run(
        BatchRunner(ExperimentStore(root), protocol).run(
            phase="pilot",
            task_ids=(protocol.tasks[0].task_id,),
            batch_id="pilot-runtime",
            executor_factory=lambda spec: PilotExecutor(status),
        )
    )
    return protocol


def _run_script(root: Path, protocol_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--batch-id",
            "pilot-runtime",
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


def test_pilot_runtime_gate_accepts_complete_evidence_and_covers_all_arms(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiments"
    protocol_path = tmp_path / "protocol.json"
    protocol = _build_pilot(root, protocol_path)

    result = load_pilot_runtime_gate(
        root,
        batch_id="pilot-runtime",
        protocol=protocol,
    )

    assert result.ready is True
    assert result.observed_trials == 4
    assert result.arms == ("L0", "L1", "L2", "L3")
    assert result.outcomes == {"pass": 4}
    assert result.projected_formal_trials == 96


def test_pilot_runtime_gate_rejects_slow_pilot(tmp_path: Path) -> None:
    root = tmp_path / "experiments"
    protocol_path = tmp_path / "protocol.json"
    protocol = _build_pilot(root, protocol_path)
    batches = BatchStore(root)
    manifest = batches.load("pilot-runtime")
    updated = datetime.fromisoformat(manifest.updated_at)
    manifest = replace(
        manifest,
        created_at=(updated - timedelta(hours=5)).isoformat(),
    )
    batches.write(manifest)

    result = load_pilot_runtime_gate(
        root,
        batch_id="pilot-runtime",
        protocol=protocol,
    )

    assert result.ready is False
    assert result.elapsed_hours == 5.0
    assert any("pilot 用时" in reason for reason in result.block_reasons)


def test_pilot_runtime_gate_rejects_model_or_tool_errors(tmp_path: Path) -> None:
    root = tmp_path / "experiments"
    protocol_path = tmp_path / "protocol.json"
    protocol = _build_pilot(root, protocol_path, status="error")

    result = load_pilot_runtime_gate(
        root,
        batch_id="pilot-runtime",
        protocol=protocol,
    )

    assert result.ready is False
    assert result.outcomes == {"error": 4}
    assert sum("记录了 error" in reason for reason in result.block_reasons) == 4


def test_pilot_runtime_gate_reports_observed_records_while_running(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiments"
    protocol_path = tmp_path / "protocol.json"
    protocol = _build_pilot(root, protocol_path)
    batches = BatchStore(root)
    manifest = batches.load("pilot-runtime")
    running = replace(manifest, status="running")
    batches.write(running)

    result = load_pilot_runtime_gate(
        root,
        batch_id="pilot-runtime",
        protocol=protocol,
    )

    assert result.ready is False
    assert result.observed_trials == 4
    assert result.outcomes == {"pass": 4}
    assert any("尚未完成" in reason for reason in result.block_reasons)


def test_verify_pilot_runtime_cli_returns_machine_readable_decision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiments"
    protocol_path = tmp_path / "protocol.json"
    _build_pilot(root, protocol_path)

    completed = _run_script(root, protocol_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ready"] is True
    assert payload["batch_id"] == "pilot-runtime"
    assert payload["block_reasons"] == []
