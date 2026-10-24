from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

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
ARCHIVE_ROOT = PROJECT_ROOT / ".bench-experiments"
ARCHIVE_V8_PILOT = "m9-local-qwen25-3b-temp0-512-pilot-live-r1"
ARCHIVE_V8_PROTOCOL = (
    PROJECT_ROOT / "bench/protocols/multi-agent-first-batch-v8-local.json"
)


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


def _baseline_pilot_protocol(path: Path, *, pilot_repeats: int):
    base = load_protocol(PROTOCOL)
    protocol = replace(
        base,
        repeats=replace(base.repeats, pilot=pilot_repeats),
    )
    path.write_text(
        json.dumps(protocol.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return protocol


def _build_baseline_pilot(
    root: Path,
    protocol_path: Path,
    *,
    pilot_repeats: int,
    statuses_by_arm: dict[str, str],
):
    protocol = _baseline_pilot_protocol(
        protocol_path,
        pilot_repeats=pilot_repeats,
    )

    class StatusByArmExecutor:
        async def execute(self, spec, trial_index: int):
            status = statuses_by_arm.get(spec.config["arm"], "fail")
            return TrialEvidence(
                status=status,
                node_path=("step:0:run_tests",),
                final_text="pilot",
                metrics=RunMetrics(
                    llm_calls=1,
                    prompt_tokens=10,
                    completion_tokens=5,
                    tool_calls=1,
                ),
                evidence={"steps": [{"action": "call_tool", "tool": "run_tests"}]},
                error="executor failed" if status == "error" else "",
            )

    asyncio.run(
        BatchRunner(ExperimentStore(root), protocol).run(
            phase="pilot",
            task_ids=(protocol.tasks[0].task_id,),
            batch_id="pilot-runtime",
            executor_factory=lambda spec: StatusByArmExecutor(),
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
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _run_script_with_baseline_args(
    root: Path,
    protocol_path: Path,
) -> subprocess.CompletedProcess[str]:
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
            "--min-baseline-trials",
            "1",
            "--baseline-pass-rate-floor",
            "-0.01",
            "--baseline-pass-rate-ceiling",
            "1.01",
            "--baseline-preferred-min",
            "0",
            "--baseline-preferred-max",
            "1",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_pilot_runtime_gate_accepts_complete_evidence_and_covers_all_arms(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiments"
    protocol_path = tmp_path / "protocol.json"
    protocol = _build_baseline_pilot(
        root,
        protocol_path,
        pilot_repeats=1,
        statuses_by_arm={"L0": "pass", "L1": "pass", "L2": "pass", "L3": "pass"},
    )

    result = load_pilot_runtime_gate(
        root,
        batch_id="pilot-runtime",
        protocol=protocol,
        min_baseline_trials=1,
            baseline_pass_rate_floor=-0.01,
            baseline_pass_rate_ceiling=1.01,
            baseline_preferred_range=(0.0, 1.0),
    )

    assert result.ready is True
    assert result.observed_trials == 4
    assert result.arms == ("L0", "L1", "L2", "L3")
    assert result.outcomes == {"pass": 4}
    assert result.projected_formal_trials == 96
    # 该 pilot 的 L0 只有 1 条有效样本，区分度信息本身在专用测试中验证。


def test_pilot_runtime_gate_requires_minimum_baseline_distinction_samples(
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

    assert result.ready is False
    assert result.baseline_arm == "L0"
    assert result.baseline_valid_trials == 1
    assert any("无法评估区分度" in reason for reason in result.block_reasons)


def test_pilot_runtime_gate_rejects_baseline_floor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiments"
    protocol_path = tmp_path / "protocol.json"
    protocol = _build_baseline_pilot(
        root,
        protocol_path,
        pilot_repeats=3,
        statuses_by_arm={arm: "fail" for arm in ("L0", "L1", "L2", "L3")},
    )

    result = load_pilot_runtime_gate(
        root,
        batch_id="pilot-runtime",
        protocol=protocol,
    )

    assert result.ready is False
    assert result.baseline_valid_trials == 3
    assert result.baseline_pass_rate == 0.0
    assert any("地板" in reason for reason in result.block_reasons)


def test_pilot_runtime_gate_rejects_baseline_ceiling(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiments"
    protocol_path = tmp_path / "protocol.json"
    protocol = _build_baseline_pilot(
        root,
        protocol_path,
        pilot_repeats=3,
        statuses_by_arm={arm: "pass" for arm in ("L0", "L1", "L2", "L3")},
    )

    result = load_pilot_runtime_gate(
        root,
        batch_id="pilot-runtime",
        protocol=protocol,
    )

    assert result.ready is False
    assert result.baseline_pass_rate == 1.0
    assert any("天花板" in reason for reason in result.block_reasons)


def test_pilot_runtime_gate_accepts_mid_range_baseline_with_soft_warning(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiments"
    protocol_path = tmp_path / "protocol.json"
    protocol = _build_baseline_pilot(
        root,
        protocol_path,
        pilot_repeats=5,
        statuses_by_arm={"L0": "fail"},  # 其他 arm 默认 fail，L0 也是 fail
    )
    # 重新构造：L0 通过 1/5 = 20%，在建议区间边界内
    class OnePassInFiveExecutor:
        calls = 0

        async def execute(self, spec, trial_index: int):
            type(self).calls += 1
            status = "pass" if self.calls == 1 else "fail"
            return TrialEvidence(
                status=status,
                node_path=("step:0:run_tests",),
                final_text="pilot",
                metrics=RunMetrics(
                    llm_calls=1,
                    prompt_tokens=10,
                    completion_tokens=5,
                    tool_calls=1,
                ),
                evidence={"steps": [{"action": "call_tool", "tool": "run_tests"}]},
            )

    asyncio.run(
        BatchRunner(ExperimentStore(root / "mid"), protocol).run(
            phase="pilot",
            task_ids=(protocol.tasks[0].task_id,),
            batch_id="pilot-mid",
            executor_factory=lambda spec: OnePassInFiveExecutor(),
        )
    )

    result = load_pilot_runtime_gate(
        root / "mid",
        batch_id="pilot-mid",
        protocol=protocol,
    )

    assert result.ready is True
    assert result.baseline_valid_trials == 5
    assert result.baseline_pass_rate == 0.2
    assert result.warnings == ()


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
    _build_baseline_pilot(
        root,
        protocol_path,
        pilot_repeats=3,
        statuses_by_arm={"L0": "pass", "L1": "fail", "L2": "fail", "L3": "fail"},
    )

    completed = _run_script_with_baseline_args(root, protocol_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ready"] is True
    assert payload["batch_id"] == "pilot-runtime"
    assert payload["block_reasons"] == []


def test_pilot_runtime_gate_replays_frozen_v8_pilot_as_not_distinct(
) -> None:
    """回溯资产：v8 历史 pilot 只有 1 条 L0 且全 fail，门禁必须挡下。"""

    if not (ARCHIVE_ROOT / "_batches" / ARCHIVE_V8_PILOT).is_dir():
        pytest.skip("本地实验归档不可用")

    result = load_pilot_runtime_gate(
        ARCHIVE_ROOT,
        batch_id=ARCHIVE_V8_PILOT,
        protocol=load_protocol(ARCHIVE_V8_PROTOCOL),
    )

    assert result.ready is False
    assert result.baseline_arm == "L0"
    assert result.baseline_valid_trials == 1
    assert result.baseline_pass_rate == 0.0
    assert any(
        "基线 L0 有效样本 1 < 3" in reason for reason in result.block_reasons
    )
