"""冻结的多智能体实验协议。

协议是任务、档位、模型、预算、重复次数、排除规则和报告口径的唯一事实源。
Run Record 只记录某次 Trial 的真实发生；协议回答的是这些 Trial 为什么可比较。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from bench.arms import arm_names
from bench.experiment.types import ExperimentSpec

PROTOCOL_SCHEMA_VERSION = 1
DEFAULT_PROTOCOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "protocols"
    / "multi-agent-first-batch-v3.json"
)

Phase = Literal["pilot", "baseline", "formal"]
BudgetAxis = Literal["cost", "tokens"]


def _json_hash(payload: Any) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise TypeError(f"protocol.{key} 必须是对象")
    return value


def _sequence(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise TypeError(f"protocol.{key} 必须是数组")
    return value


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"protocol.{key} 必须是非空字符串")
    return value.strip()


def _text_item(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"protocol.{label} 必须是非空字符串")
    return value.strip()


def _mapping_item(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"protocol.{label} 项必须是对象")
    return value


def _integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"protocol.{key} 必须是整数")
    return value


def _number(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"protocol.{key} 必须是数字")
    return float(value)


def _optional_number(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    return _number(payload, key)


def _boolean(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise TypeError(f"protocol.{key} 必须是布尔值")
    return value


@dataclass(frozen=True)
class ProtocolTask:
    task_id: str
    origin: Literal["E1", "E3"]
    task_file: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProtocolTask":
        origin = _text(payload, "origin")
        if origin not in {"E1", "E3"}:
            raise ValueError(f"protocol task origin 未知: {origin!r}")
        return cls(
            task_id=_text(payload, "task_id"),
            origin=origin,
            task_file=_text(payload, "task_file"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "origin": self.origin,
            "task_file": self.task_file,
        }


@dataclass(frozen=True)
class ProtocolArm:
    name: str
    version: str
    description: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProtocolArm":
        name = _text(payload, "name")
        if not name.startswith("L"):
            raise ValueError(f"protocol arm 名称非法: {name!r}")
        return cls(
            name=name,
            version=_text(payload, "version"),
            description=_text(payload, "description"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
        }


@dataclass(frozen=True)
class ProtocolModel:
    provider: str
    model: str
    base_url: str
    temperature: float
    timeout: float
    max_tokens: int
    cost_signal: bool

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProtocolModel":
        return cls(
            provider=_text(payload, "provider"),
            model=_text(payload, "model"),
            base_url=_text(payload, "base_url"),
            temperature=_number(payload, "temperature"),
            timeout=_number(payload, "timeout"),
            max_tokens=_integer(payload, "max_tokens"),
            cost_signal=_boolean(payload, "cost_signal"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "timeout": self.timeout,
            "max_tokens": self.max_tokens,
            "cost_signal": self.cost_signal,
        }

    def experiment_record(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "timeout": self.timeout,
            "max_tokens": self.max_tokens,
        }


@dataclass(frozen=True)
class BudgetCap:
    max_cost_usd: float | None
    max_steps: int
    max_tokens: int

    def __post_init__(self) -> None:
        if self.max_cost_usd is not None and self.max_cost_usd < 0:
            raise ValueError("budget max_cost_usd 不能为负")
        if self.max_steps < 1:
            raise ValueError("budget max_steps 必须 >= 1")
        if self.max_tokens < 0:
            raise ValueError("budget max_tokens 不能为负")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BudgetCap":
        return cls(
            max_cost_usd=_optional_number(payload, "max_cost_usd"),
            max_steps=_integer(payload, "max_steps"),
            max_tokens=_integer(payload, "max_tokens"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_cost_usd": self.max_cost_usd,
            "max_steps": self.max_steps,
            "max_tokens": self.max_tokens,
        }


@dataclass(frozen=True)
class ProtocolBudget:
    axis: BudgetAxis
    b1: BudgetCap
    b3: BudgetCap

    def __post_init__(self) -> None:
        if self.axis not in {"cost", "tokens"}:
            raise ValueError(f"protocol.budget.axis 未知: {self.axis!r}")
        if self.axis == "tokens":
            if self.b1.max_tokens <= 0 or self.b3.max_tokens <= 0:
                raise ValueError("token 主轴的 B1/B3 max_tokens 必须 > 0")
        elif self.b1.max_cost_usd is None or self.b3.max_cost_usd is None:
            raise ValueError("cost 主轴的 B1/B3 max_cost_usd 不能为空")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProtocolBudget":
        axis = _text(payload, "axis")
        if axis not in {"cost", "tokens"}:
            raise ValueError(f"protocol.budget.axis 未知: {axis!r}")
        return cls(
            axis=axis,
            b1=BudgetCap.from_dict(_mapping(payload, "b1")),
            b3=BudgetCap.from_dict(_mapping(payload, "b3")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "b1": self.b1.to_dict(),
            "b3": self.b3.to_dict(),
        }


@dataclass(frozen=True)
class ProtocolRepeats:
    pilot: int
    baseline: int
    formal: int

    def __post_init__(self) -> None:
        if min(self.pilot, self.baseline, self.formal) < 1:
            raise ValueError("protocol.repeats 必须全部 >= 1")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProtocolRepeats":
        return cls(
            pilot=_integer(payload, "pilot"),
            baseline=_integer(payload, "baseline"),
            formal=_integer(payload, "formal"),
        )

    def for_phase(self, phase: Phase) -> int:
        if phase not in {"pilot", "baseline", "formal"}:
            raise ValueError(f"未知实验阶段: {phase!r}")
        return int(getattr(self, phase))

    def to_dict(self) -> dict[str, int]:
        return {
            "pilot": self.pilot,
            "baseline": self.baseline,
            "formal": self.formal,
        }


@dataclass(frozen=True)
class ProtocolRetry:
    max_error_retries: int
    retry_on: tuple[str, ...]
    do_not_retry_on: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.max_error_retries < 0:
            raise ValueError("protocol.retry.max_error_retries 不能为负")
        overlap = set(self.retry_on) & set(self.do_not_retry_on)
        if overlap:
            raise ValueError(f"retry_on 与 do_not_retry_on 重叠: {sorted(overlap)}")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProtocolRetry":
        return cls(
            max_error_retries=_integer(payload, "max_error_retries"),
            retry_on=tuple(
                _text_item(item, "retry_on")
                for item in _sequence(payload, "retry_on")
            ),
            do_not_retry_on=tuple(
                _text_item(item, "do_not_retry_on")
                for item in _sequence(payload, "do_not_retry_on")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_error_retries": self.max_error_retries,
            "retry_on": list(self.retry_on),
            "do_not_retry_on": list(self.do_not_retry_on),
        }


@dataclass(frozen=True)
class ProtocolExclusions:
    excluded_outcomes: tuple[str, ...]
    exclude_from_pass_rate: tuple[str, ...]
    invalid_task_fails_first: bool

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProtocolExclusions":
        return cls(
            excluded_outcomes=tuple(
                _text_item(item, "excluded_outcomes")
                for item in _sequence(payload, "excluded_outcomes")
            ),
            exclude_from_pass_rate=tuple(
                _text_item(item, "exclude_from_pass_rate")
                for item in _sequence(payload, "exclude_from_pass_rate")
            ),
            invalid_task_fails_first=_boolean(payload, "invalid_task_fails_first"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "excluded_outcomes": list(self.excluded_outcomes),
            "exclude_from_pass_rate": list(self.exclude_from_pass_rate),
            "invalid_task_fails_first": self.invalid_task_fails_first,
        }


@dataclass(frozen=True)
class ProtocolReporting:
    statistical_claims: bool
    directional_until_samples: int
    primary_metric: str
    cost_unavailable_reason: str
    group_by: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.directional_until_samples < 1:
            raise ValueError("directional_until_samples 必须 >= 1")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProtocolReporting":
        return cls(
            statistical_claims=_boolean(payload, "statistical_claims"),
            directional_until_samples=_integer(payload, "directional_until_samples"),
            primary_metric=_text(payload, "primary_metric"),
            cost_unavailable_reason=_text(payload, "cost_unavailable_reason"),
            group_by=tuple(
                _text_item(item, "group_by")
                for item in _sequence(payload, "group_by")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "statistical_claims": self.statistical_claims,
            "directional_until_samples": self.directional_until_samples,
            "primary_metric": self.primary_metric,
            "cost_unavailable_reason": self.cost_unavailable_reason,
            "group_by": list(self.group_by),
        }


@dataclass(frozen=True)
class ExperimentProtocol:
    schema_version: int
    protocol_id: str
    version: str
    frozen_at: str
    tasks: tuple[ProtocolTask, ...]
    arms: tuple[ProtocolArm, ...]
    model: ProtocolModel
    budget: ProtocolBudget
    repeats: ProtocolRepeats
    retry: ProtocolRetry
    exclusions: ProtocolExclusions
    reporting: ProtocolReporting

    def __post_init__(self) -> None:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("protocol.tasks 包含重复 task_id")
        declared_arm_names = [arm.name for arm in self.arms]
        if len(declared_arm_names) != len(set(declared_arm_names)):
            raise ValueError("protocol.arms 包含重复 arm")
        registered = set(arm_names())
        unknown = tuple(name for name in declared_arm_names if name not in registered)
        if unknown:
            raise ValueError(f"protocol 包含未登记 arm: {', '.join(unknown)}")
        missing = tuple(name for name in arm_names() if name not in declared_arm_names)
        if missing:
            raise ValueError(f"protocol 缺少已登记 arm: {', '.join(missing)}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "protocol_id": self.protocol_id,
            "version": self.version,
            "frozen_at": self.frozen_at,
            "tasks": [task.to_dict() for task in self.tasks],
            "arms": [arm.to_dict() for arm in self.arms],
            "model": self.model.to_dict(),
            "budget": self.budget.to_dict(),
            "repeats": self.repeats.to_dict(),
            "retry": self.retry.to_dict(),
            "exclusions": self.exclusions.to_dict(),
            "reporting": self.reporting.to_dict(),
        }

    @property
    def config_hash(self) -> str:
        return _json_hash(self.identity_payload())

    def _task(self, task_id: str) -> ProtocolTask:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise ValueError(f"协议不包含 task_id: {task_id!r}")

    def _arm(self, name: str) -> ProtocolArm:
        for arm in self.arms:
            if arm.name == name:
                return arm
        raise ValueError(f"协议不包含 arm: {name!r}")

    def experiment_spec(
        self,
        task_id: str,
        arm_name: str,
        *,
        phase: Phase = "baseline",
        batch_id: str = "protocol-preview",
    ) -> ExperimentSpec:
        """把一个协议单元映射为独立 Experiment；repeat 是该 Experiment 的 Trial。"""
        task = self._task(task_id)
        arm = self._arm(arm_name)
        if not batch_id.strip():
            raise ValueError("batch_id 不能为空")
        cell = _json_hash(
            {
                "protocol_hash": self.config_hash,
                "batch_id": batch_id,
                "phase": phase,
                "task_id": task.task_id,
                "arm": arm.name,
            }
        )
        budget = self.budget.b1.to_dict()
        return ExperimentSpec(
            experiment_id=f"exp-{cell[:16]}",
            task=f"bench:{task.task_id}",
            adapter=f"bench:{arm.name}",
            trials=self.repeats.for_phase(phase),
            model=self.model.experiment_record(),
            config={
                "protocol_id": self.protocol_id,
                "protocol_version": self.version,
                "protocol_hash": self.config_hash,
                "phase": phase,
                "batch_id": batch_id,
                "task_id": task.task_id,
                "tasks_dir": "bench/tasks",
                "arm": arm.name,
                "arm_version": arm.version,
                "budget_tier": "B1",
                "budget": budget,
                "prompt_version": "bench-arm-prompts-v1",
            },
            metadata={
                "entrypoint": "bench/experiment/protocol.py",
                "task_origin": task.origin,
                "retry": self.retry.to_dict(),
                "exclusions": self.exclusions.to_dict(),
                "reporting": self.reporting.to_dict(),
            },
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ExperimentProtocol":
        schema_version = _integer(payload, "schema_version")
        if schema_version != PROTOCOL_SCHEMA_VERSION:
            raise ValueError(
                f"不支持的协议 schema_version: {schema_version}"
                f"（只认 {PROTOCOL_SCHEMA_VERSION}）"
            )
        protocol = cls(
            schema_version=schema_version,
            protocol_id=_text(payload, "protocol_id"),
            version=_text(payload, "version"),
            frozen_at=_text(payload, "frozen_at"),
            tasks=tuple(
                ProtocolTask.from_dict(_mapping_item(item, "tasks"))
                for item in _sequence(payload, "tasks")
            ),
            arms=tuple(
                ProtocolArm.from_dict(_mapping_item(item, "arms"))
                for item in _sequence(payload, "arms")
            ),
            model=ProtocolModel.from_dict(_mapping(payload, "model")),
            budget=ProtocolBudget.from_dict(_mapping(payload, "budget")),
            repeats=ProtocolRepeats.from_dict(_mapping(payload, "repeats")),
            retry=ProtocolRetry.from_dict(_mapping(payload, "retry")),
            exclusions=ProtocolExclusions.from_dict(_mapping(payload, "exclusions")),
            reporting=ProtocolReporting.from_dict(_mapping(payload, "reporting")),
        )
        if not protocol.tasks:
            raise ValueError("protocol.tasks 不能为空")
        if not protocol.arms:
            raise ValueError("protocol.arms 不能为空")
        return protocol

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "config_hash": self.config_hash}


def protocol_from_dict(payload: dict[str, Any]) -> ExperimentProtocol:
    return ExperimentProtocol.from_dict(payload)


def load_protocol(path: Path | str = DEFAULT_PROTOCOL_PATH) -> ExperimentProtocol:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("协议根节点必须是对象")
    return protocol_from_dict(payload)


__all__ = [
    "BudgetCap",
    "DEFAULT_PROTOCOL_PATH",
    "ExperimentProtocol",
    "PROTOCOL_SCHEMA_VERSION",
    "ProtocolArm",
    "ProtocolBudget",
    "ProtocolExclusions",
    "ProtocolModel",
    "ProtocolReporting",
    "ProtocolRepeats",
    "ProtocolRetry",
    "ProtocolTask",
    "load_protocol",
    "protocol_from_dict",
]
