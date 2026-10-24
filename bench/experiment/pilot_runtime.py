"""Pilot runtime gate for deciding whether a full live batch is practical."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bench.experiment.batch import BatchStore
from bench.experiment.protocol import ExperimentProtocol
from bench.experiment.store import ExperimentStore


@dataclass(frozen=True)
class PilotRuntimeGate:
    ready: bool
    batch_id: str
    batch_status: str
    elapsed_hours: float
    expected_pilot_trials: int
    observed_trials: int
    projected_formal_trials: int
    projected_formal_hours: float
    max_pilot_hours: float
    max_projected_formal_hours: float
    outcomes: dict[str, int]
    arms: tuple[str, ...]
    block_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "batch_id": self.batch_id,
            "batch_status": self.batch_status,
            "elapsed_hours": self.elapsed_hours,
            "expected_pilot_trials": self.expected_pilot_trials,
            "observed_trials": self.observed_trials,
            "projected_formal_trials": self.projected_formal_trials,
            "projected_formal_hours": self.projected_formal_hours,
            "max_pilot_hours": self.max_pilot_hours,
            "max_projected_formal_hours": self.max_projected_formal_hours,
            "outcomes": dict(sorted(self.outcomes.items())),
            "arms": list(self.arms),
            "block_reasons": list(self.block_reasons),
        }


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Batch Manifest {field} 不是 ISO 时间: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Batch Manifest {field} 缺少时区: {value!r}")
    return parsed


def load_pilot_runtime_gate(
    output_root: Path | str,
    *,
    batch_id: str,
    protocol: ExperimentProtocol,
    max_pilot_hours: float = 4.0,
    max_projected_formal_hours: float = 72.0,
) -> PilotRuntimeGate:
    if max_pilot_hours <= 0 or max_projected_formal_hours <= 0:
        raise ValueError("运行时间门禁必须是正数")

    root = Path(output_root).resolve()
    manifest = BatchStore(root).load(batch_id)
    if manifest.protocol_hash != protocol.config_hash:
        raise ValueError(
            "协议哈希不一致: "
            f"{protocol.config_hash} != {manifest.protocol_hash}"
        )

    created = _timestamp(manifest.created_at, "created_at")
    updated = _timestamp(manifest.updated_at, "updated_at")
    elapsed_hours = max(0.0, (updated - created).total_seconds() / 3600.0)
    reasons: list[str] = []
    if manifest.phase != "pilot":
        reasons.append(f"批次 phase={manifest.phase!r}，不是 pilot")
    if manifest.status != "completed":
        reasons.append(f"批次状态为 {manifest.status!r}，尚未完成")
    if manifest.max_concurrency != 1:
        reasons.append("pilot 并发度不是 1，不能外推串行正式批次耗时")

    expected_pilot_trials = sum(
        cell.expected_trials for cell in manifest.cells
    )
    protocol_arms = tuple(arm.name for arm in protocol.arms)
    manifest_arms = tuple(sorted(cell.arm for cell in manifest.cells))
    missing_arms = sorted(set(protocol_arms) - set(manifest_arms))
    if missing_arms:
        reasons.append(f"pilot 缺少协议 arm: {', '.join(missing_arms)}")
    if len(set(manifest_arms)) != len(protocol_arms):
        reasons.append("pilot arm 数量或身份与协议不一致")

    records = []
    experiments = ExperimentStore(root)
    for cell in manifest.cells:
        if cell.status != "completed":
            reasons.append(f"{cell.cell_id} 单元未完成")
            continue
        cell_records = experiments.load_experiment_records(cell.experiment_id)
        if len(cell_records) != cell.expected_trials:
            reasons.append(
                f"{cell.cell_id} Trial 数 {len(cell_records)} "
                f"!= {cell.expected_trials}"
            )
        for record in cell_records:
            label = f"{cell.cell_id} trial-{record.trial_index:03d}"
            if record.status == "error" or record.error.strip():
                reasons.append(f"{label} 记录了 error")
            if record.metrics.llm_calls <= 0:
                reasons.append(f"{label} 没有真实模型调用")
            if record.metrics.tool_calls <= 0 or not record.node_path:
                reasons.append(f"{label} 没有工具轨迹")
            records.append(record)

    outcomes: dict[str, int] = {}
    for record in records:
        outcomes[record.status] = outcomes.get(record.status, 0) + 1

    projected_formal_trials = (
        len(protocol.tasks) * len(protocol.arms) * protocol.repeats.formal
    )
    projected_formal_hours = (
        elapsed_hours / len(records) * projected_formal_trials
        if records
        else 0.0
    )
    if elapsed_hours > max_pilot_hours:
        reasons.append(
            f"pilot 用时 {elapsed_hours:.3f}h 超过 {max_pilot_hours:.3f}h"
        )
    if projected_formal_hours > max_projected_formal_hours:
        reasons.append(
            f"正式批次投影 {projected_formal_hours:.3f}h "
            f"超过 {max_projected_formal_hours:.3f}h"
        )

    return PilotRuntimeGate(
        ready=not reasons,
        batch_id=manifest.batch_id,
        batch_status=manifest.status,
        elapsed_hours=round(elapsed_hours, 3),
        expected_pilot_trials=expected_pilot_trials,
        observed_trials=len(records),
        projected_formal_trials=projected_formal_trials,
        projected_formal_hours=round(projected_formal_hours, 3),
        max_pilot_hours=max_pilot_hours,
        max_projected_formal_hours=max_projected_formal_hours,
        outcomes=outcomes,
        arms=tuple(sorted(set(manifest_arms))),
        block_reasons=tuple(reasons),
    )


__all__ = ["PilotRuntimeGate", "load_pilot_runtime_gate"]
