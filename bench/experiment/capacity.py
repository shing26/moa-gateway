"""Capacity recovery gate for live multi-agent pilots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.experiment.batch import BatchManifest, BatchStore
from bench.experiment.store import ExperimentStore
from bench.experiment.types import RunRecord

CAPACITY_GATE_SUCCESSES = 2
_VALID_PILOT_OUTCOMES = frozenset({"pass", "fail"})


def _contains_rate_limit(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("rate_limited") is True:
            return True
        try:
            if int(value.get("status_code", 0) or 0) == 429:
                return True
        except (TypeError, ValueError):
            pass
        error = str(value.get("error", "") or "")
        if "429" in error or "rate limit" in error.lower():
            return True
        return any(
            _contains_rate_limit(value[key])
            for key in ("llm_retry_events", "retry_attempts", "evidence")
            if key in value
        )
    if isinstance(value, list):
        return any(_contains_rate_limit(item) for item in value)
    return False


def _event_count(record: RunRecord) -> int:
    events = record.evidence.get("llm_retry_events")
    return len(events) if isinstance(events, list) else 0


@dataclass(frozen=True)
class CapacityPilotObservation:
    batch_id: str
    observed_at: str
    arm: str
    status: str
    llm_calls: int
    subtask_count: int
    retry_event_count: int
    explicit_retry_telemetry: bool
    rate_limited: bool
    eligible: bool
    l2_multicall: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "observed_at": self.observed_at,
            "arm": self.arm,
            "status": self.status,
            "llm_calls": self.llm_calls,
            "subtask_count": self.subtask_count,
            "retry_event_count": self.retry_event_count,
            "explicit_retry_telemetry": self.explicit_retry_telemetry,
            "rate_limited": self.rate_limited,
            "eligible": self.eligible,
            "l2_multicall": self.l2_multicall,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class CapacityGateResult:
    ready: bool
    required_consecutive_successes: int
    current_success_streak: int
    l2_in_current_streak: bool
    reasons: tuple[str, ...]
    observations: tuple[CapacityPilotObservation, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "required_consecutive_successes": (self.required_consecutive_successes),
            "current_success_streak": self.current_success_streak,
            "l2_in_current_streak": self.l2_in_current_streak,
            "reasons": list(self.reasons),
            "observations": [item.to_dict() for item in self.observations],
        }


def observe_capacity_pilot(
    manifest: BatchManifest,
    records: tuple[RunRecord, ...],
    *,
    protocol_hash: str,
) -> CapacityPilotObservation:
    reasons: list[str] = []
    if manifest.status != "completed":
        reasons.append(f"批次状态为 {manifest.status}，不是 completed")
    if manifest.phase != "pilot":
        reasons.append(f"批次阶段为 {manifest.phase}，不是 pilot")
    if manifest.protocol_hash != protocol_hash:
        reasons.append("协议哈希与待恢复批次不一致")
    if len(manifest.cells) != 1:
        reasons.append(f"单次容量 pilot 必须只有一个单元，实际 {len(manifest.cells)}")

    cell = manifest.cells[0] if len(manifest.cells) == 1 else None
    if cell is not None:
        if cell.status != "completed":
            reasons.append(f"单元状态为 {cell.status}，不是 completed")
        if cell.expected_trials != 1:
            reasons.append("容量 pilot 每个单元只能执行 1 次 Trial")
    if len(records) != 1:
        reasons.append(f"容量 pilot 必须落盘 1 条 Run Record，实际 {len(records)}")

    record = records[0] if len(records) == 1 else None
    status = record.status if record is not None else ""
    arm = cell.arm if cell is not None else ""
    if record is not None and status not in _VALID_PILOT_OUTCOMES:
        reasons.append(f"Run Record 结局为 {status}，不能作为有效容量样本")

    explicit = bool(
        record is not None and isinstance(record.evidence.get("llm_retry_events"), list)
    )
    if not explicit:
        reasons.append("Run Record 缺少 llm_retry_events，无法排除被重试掩盖的 429")

    rate_limited = bool(record is not None and _contains_rate_limit(record.evidence))
    if record is not None and (
        "429" in record.error or "rate limit" in record.error.lower()
    ):
        rate_limited = True
    if rate_limited:
        reasons.append("Run Record 含 429/RateLimit，连续成功门槛重新计数")

    llm_calls = record.metrics.llm_calls if record is not None else 0
    subtask_count = (
        int(record.evidence.get("subtask_count", 0) or 0) if record is not None else 0
    )
    if llm_calls < 1:
        reasons.append("没有真实 LLM 调用")

    l2_multicall = arm in {"L2", "L3"} and llm_calls >= 2 and subtask_count >= 1
    eligible = not reasons
    return CapacityPilotObservation(
        batch_id=manifest.batch_id,
        observed_at=manifest.updated_at,
        arm=arm,
        status=status,
        llm_calls=llm_calls,
        subtask_count=subtask_count,
        retry_event_count=_event_count(record) if record is not None else 0,
        explicit_retry_telemetry=explicit,
        rate_limited=rate_limited,
        eligible=eligible,
        l2_multicall=l2_multicall,
        reasons=tuple(reasons),
    )


def evaluate_capacity_gate(
    observations: tuple[CapacityPilotObservation, ...] | list[CapacityPilotObservation],
) -> CapacityGateResult:
    ordered = tuple(
        sorted(observations, key=lambda item: (item.observed_at, item.batch_id))
    )
    streak: list[CapacityPilotObservation] = []
    for item in ordered:
        if item.eligible:
            streak.append(item)
        else:
            streak = []

    l2_in_streak = any(item.l2_multicall for item in streak)
    reasons: list[str] = []
    if len(streak) < CAPACITY_GATE_SUCCESSES:
        reasons.append(f"当前连续有效 pilot {len(streak)} < {CAPACITY_GATE_SUCCESSES}")
    if not l2_in_streak:
        reasons.append("当前连续成功段中没有 L2 多调用 pilot")
    ready = not reasons
    return CapacityGateResult(
        ready=ready,
        required_consecutive_successes=CAPACITY_GATE_SUCCESSES,
        current_success_streak=len(streak),
        l2_in_current_streak=l2_in_streak,
        reasons=tuple(reasons),
        observations=ordered,
    )


def load_capacity_gate(
    output_root: Path | str,
    *,
    batch_ids: tuple[str, ...] | list[str],
    protocol_hash: str,
) -> CapacityGateResult:
    root = Path(output_root).resolve()
    batches = BatchStore(root)
    experiments = ExperimentStore(root)
    observations: list[CapacityPilotObservation] = []
    for batch_id in batch_ids:
        manifest = batches.load(batch_id)
        records = (
            experiments.load_experiment_records(manifest.cells[0].experiment_id)
            if len(manifest.cells) == 1
            else ()
        )
        observations.append(
            observe_capacity_pilot(
                manifest,
                tuple(records),
                protocol_hash=protocol_hash,
            )
        )
    return evaluate_capacity_gate(observations)


__all__ = [
    "CAPACITY_GATE_SUCCESSES",
    "CapacityGateResult",
    "CapacityPilotObservation",
    "evaluate_capacity_gate",
    "load_capacity_gate",
    "observe_capacity_pilot",
]
