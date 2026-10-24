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
    warnings: tuple[str, ...]
    baseline_arm: str
    baseline_valid_trials: int
    baseline_pass_rate: float | None
    min_baseline_trials: int
    baseline_pass_rate_floor: float
    baseline_pass_rate_ceiling: float
    baseline_preferred_range: tuple[float, float]

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
            "warnings": list(self.warnings),
            "baseline_arm": self.baseline_arm,
            "baseline_valid_trials": self.baseline_valid_trials,
            "baseline_pass_rate": self.baseline_pass_rate,
            "min_baseline_trials": self.min_baseline_trials,
            "baseline_pass_rate_floor": self.baseline_pass_rate_floor,
            "baseline_pass_rate_ceiling": self.baseline_pass_rate_ceiling,
            "baseline_preferred_range": list(self.baseline_preferred_range),
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
    min_baseline_trials: int = 3,
    baseline_pass_rate_floor: float = 0.0,
    baseline_pass_rate_ceiling: float = 1.0,
    baseline_preferred_range: tuple[float, float] = (0.2, 0.8),
) -> PilotRuntimeGate:
    if max_pilot_hours <= 0 or max_projected_formal_hours <= 0:
        raise ValueError("运行时间门禁必须是正数")
    if min_baseline_trials < 1:
        raise ValueError("基线最小有效样本数必须 >= 1")
    floor = float(baseline_pass_rate_floor)
    ceiling = float(baseline_pass_rate_ceiling)
    if not 0.0 <= floor < ceiling <= 1.0:
        raise ValueError("基线通过率硬线必须满足 0 <= floor < ceiling <= 1")
    if not (
        floor <= baseline_preferred_range[0]
        < baseline_preferred_range[1] <= ceiling
    ):
        raise ValueError("基线建议区间必须落在硬线区间内")

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
    baseline_arm = (
        protocol.statistics.baseline_arm
        if protocol.statistics is not None
        else "L0"
    )
    baseline_records = []
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
            if cell.arm == baseline_arm:
                baseline_records.append(record)

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

    # 区分度门禁：基线若贴地板/天花板，配对差无法区分"编排无增益"与
    # "基线能力不足/任务过易"，正式批次的 pass_rate 比较会空转。
    baseline_valid = [
        record for record in baseline_records if record.status != "error"
    ]
    baseline_passes = sum(
        record.status == "pass" for record in baseline_valid
    )
    baseline_pass_rate = (
        baseline_passes / len(baseline_valid) if baseline_valid else None
    )
    warnings: list[str] = []
    if len(baseline_valid) < min_baseline_trials:
        reasons.append(
            f"基线 {baseline_arm} 有效样本 {len(baseline_valid)} "
            f"< {min_baseline_trials}，无法评估区分度"
        )
    elif baseline_pass_rate is not None:
        if baseline_pass_rate <= floor:
            reasons.append(
                f"基线 {baseline_arm} 通过率 {baseline_pass_rate:.1%} "
                f"<= {baseline_pass_rate_floor:.0%}（地板），无法区分编排增益"
                "与基线能力不足"
            )
        elif baseline_pass_rate >= ceiling:
            reasons.append(
                f"基线 {baseline_arm} 通过率 {baseline_pass_rate:.1%} "
                f">= {baseline_pass_rate_ceiling:.0%}（天花板），"
                "增益空间不足，配对检验功效受限"
            )
        elif (
            baseline_pass_rate < baseline_preferred_range[0]
            or baseline_pass_rate > baseline_preferred_range[1]
        ):
            warnings.append(
                f"基线 {baseline_arm} 通过率 {baseline_pass_rate:.1%} "
                "在建议区间 "
                f"[{baseline_preferred_range[0]:.0%}, "
                f"{baseline_preferred_range[1]:.0%}] 之外，"
                "配对检验功效可能不足"
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
        warnings=tuple(warnings),
        baseline_arm=baseline_arm,
        baseline_valid_trials=len(baseline_valid),
        baseline_pass_rate=baseline_pass_rate,
        min_baseline_trials=min_baseline_trials,
        baseline_pass_rate_floor=baseline_pass_rate_floor,
        baseline_pass_rate_ceiling=baseline_pass_rate_ceiling,
        baseline_preferred_range=baseline_preferred_range,
    )


__all__ = ["PilotRuntimeGate", "load_pilot_runtime_gate"]
