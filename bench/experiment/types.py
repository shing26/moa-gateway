"""多智能体实验台的稳定证据类型。

Experiment 是配置单位，Trial 是独立执行，Run Record 是一个 Trial 的机器可读证据。
这里刻意不依赖 Web、数据库或某个执行引擎：Collaboration 与 bench arm 都只负责把
各自运行转换成同一份 Run Record。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

SCHEMA_VERSION = 1

_SKIP_DIRS = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", ".bench-work"}
)


def _json_hash(payload: Any) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class RunMetrics:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_latency_ms: float = 0.0
    tool_calls: int = 0
    tool_errors: int = 0
    tool_arg_rejections: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "llm_latency_ms": self.llm_latency_ms,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "tool_arg_rejections": self.tool_arg_rejections,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "RunMetrics":
        data = payload or {}
        return cls(
            llm_calls=int(data.get("llm_calls", 0) or 0),
            prompt_tokens=int(data.get("prompt_tokens", 0) or 0),
            completion_tokens=int(data.get("completion_tokens", 0) or 0),
            cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
            llm_latency_ms=float(data.get("llm_latency_ms", 0.0) or 0.0),
            tool_calls=int(data.get("tool_calls", 0) or 0),
            tool_errors=int(data.get("tool_errors", 0) or 0),
            tool_arg_rejections=int(data.get("tool_arg_rejections", 0) or 0),
        )


@dataclass(frozen=True)
class WorkspaceSnapshot:
    path: str = ""
    before_hash: str = ""
    after_hash: str = ""
    files_before: int = 0
    files_after: int = 0

    @property
    def file_count(self) -> int:
        """材料化前后的文件数应一致；不一致时以执行后快照为准。"""
        return self.files_after

    @property
    def content_hash(self) -> str:
        """工作区当前内容的指纹；执行后快照优先。"""
        return self.after_hash or self.before_hash

    @property
    def snapshot_id(self) -> str:
        """不依赖绝对路径的工作区快照标识。"""
        if not self.before_hash and not self.after_hash:
            return ""
        return _json_hash(
            {
                "before_hash": self.before_hash,
                "after_hash": self.after_hash,
                "files_before": self.files_before,
                "files_after": self.files_after,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "path": self.path,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "files_before": self.files_before,
            "files_after": self.files_after,
        }


def fingerprint_workspace(root: Path | str) -> WorkspaceSnapshot:
    """对工作区内容做稳定指纹，不跟随目录内的符号链接。"""
    path = Path(root).resolve()
    if not path.exists() or not path.is_dir():
        return WorkspaceSnapshot(path=str(path))
    digest = hashlib.sha256()
    count = 0
    for item in sorted(path.rglob("*")):
        rel_parts = item.relative_to(path).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        rel = item.relative_to(path).as_posix()
        if item.is_symlink():
            digest.update(f"L:{rel}:{item.readlink()}\0".encode())
            count += 1
            continue
        if not item.is_file():
            continue
        file_digest = hashlib.sha256(item.read_bytes()).hexdigest()
        digest.update(f"F:{rel}:{file_digest}\0".encode())
        count += 1
    return WorkspaceSnapshot(
        path=str(path),
        before_hash=digest.hexdigest(),
        after_hash=digest.hexdigest(),
        files_before=count,
        files_after=count,
    )


@dataclass(frozen=True)
class ExperimentSpec:
    task: str
    adapter: str
    trials: int = 1
    experiment_id: str = ""
    workspace_ref: str = ""
    verify_command: tuple[str, ...] = ()
    model: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    parent_experiment_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.task or "").strip():
            raise ValueError("Experiment.task 不能为空")
        if not str(self.adapter or "").strip():
            raise ValueError("Experiment.adapter 不能为空")
        if int(self.trials) < 1:
            raise ValueError("Experiment.trials 必须 >= 1")

    def identity_payload(self) -> dict[str, Any]:
        """去掉 experiment_id 与执行产物后，用于受控复跑比对的配置。"""
        return {
            "task": self.task,
            "adapter": self.adapter,
            "workspace_ref": self.workspace_ref,
            "verify_command": list(self.verify_command),
            "model": dict(self.model),
            "config": dict(self.config),
            "metadata": dict(self.metadata),
        }

    @property
    def config_hash(self) -> str:
        return _json_hash(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "trials": self.trials,
            **self.identity_payload(),
            "config_hash": self.config_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ExperimentSpec":
        return cls(
            task=str(payload.get("task", "")),
            adapter=str(payload.get("adapter", "")),
            trials=int(payload.get("trials", 1) or 1),
            experiment_id=str(payload.get("experiment_id", "")),
            workspace_ref=str(payload.get("workspace_ref", "")),
            verify_command=tuple(
                str(item) for item in (payload.get("verify_command") or ())
            ),
            model=dict(payload.get("model") or {}),
            config=dict(payload.get("config") or {}),
            parent_experiment_id=str(payload.get("parent_experiment_id", "")),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class TrialEvidence:
    status: str
    plan: tuple[dict[str, Any], ...] = ()
    node_path: tuple[str, ...] = ()
    per_agent_outputs: dict[str, str] = field(default_factory=dict)
    final_text: str = ""
    metrics: RunMetrics = field(default_factory=RunMetrics)
    prompt_hashes: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    workspace: WorkspaceSnapshot = field(default_factory=WorkspaceSnapshot)
    error: str = ""


@dataclass(frozen=True)
class RunRecord:
    schema_version: int
    experiment_id: str
    trial_id: str
    trial_index: int
    created_at: str
    duration_ms: float
    task: str
    task_hash: str
    config_hash: str
    adapter: str
    status: str
    plan: tuple[dict[str, Any], ...]
    node_path: tuple[str, ...]
    per_agent_outputs: dict[str, str]
    final_text: str
    metrics: RunMetrics
    evidence: dict[str, Any]
    workspace: WorkspaceSnapshot
    error: str
    model: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    verify_command: tuple[str, ...] = ()
    prompt_hashes: tuple[str, ...] = ()
    path: Path | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "trial_id": self.trial_id,
            "trial_index": self.trial_index,
            "created_at": self.created_at,
            "duration_ms": self.duration_ms,
            "task": self.task,
            "task_hash": self.task_hash,
            "config_hash": self.config_hash,
            "adapter": self.adapter,
            "status": self.status,
            "plan": [dict(item) for item in self.plan],
            "node_path": list(self.node_path),
            "per_agent_outputs": dict(self.per_agent_outputs),
            "final_text": self.final_text,
            "metrics": self.metrics.to_dict(),
            "model": dict(self.model),
            "config": dict(self.config),
            "verify_command": list(self.verify_command),
            "prompt_hashes": list(self.prompt_hashes),
            "evidence": dict(self.evidence),
            "workspace": self.workspace.to_dict(),
            "error": self.error,
        }

    @classmethod
    def from_dict(
        cls, payload: dict[str, Any], *, path: Path | None = None
    ) -> "RunRecord":
        workspace = payload.get("workspace") or {}
        return cls(
            schema_version=int(payload.get("schema_version", 0) or 0),
            experiment_id=str(payload.get("experiment_id", "")),
            trial_id=str(payload.get("trial_id", "")),
            trial_index=int(payload.get("trial_index", 0) or 0),
            created_at=str(payload.get("created_at", "")),
            duration_ms=float(payload.get("duration_ms", 0.0) or 0.0),
            task=str(payload.get("task", "")),
            task_hash=str(payload.get("task_hash", "")),
            config_hash=str(payload.get("config_hash", "")),
            adapter=str(payload.get("adapter", "")),
            status=str(payload.get("status", "")),
            plan=tuple(
                dict(item) for item in (payload.get("plan") or ()) if isinstance(item, dict)
            ),
            node_path=tuple(str(item) for item in (payload.get("node_path") or ())),
            per_agent_outputs={
                str(key): str(value)
                for key, value in dict(payload.get("per_agent_outputs") or {}).items()
            },
            final_text=str(payload.get("final_text", "")),
            metrics=RunMetrics.from_dict(payload.get("metrics")),
            model=dict(payload.get("model") or {}),
            config=dict(payload.get("config") or {}),
            verify_command=tuple(
                str(item) for item in (payload.get("verify_command") or ())
            ),
            prompt_hashes=tuple(
                str(item) for item in (payload.get("prompt_hashes") or ())
            ),
            evidence=dict(payload.get("evidence") or {}),
            workspace=WorkspaceSnapshot(
                path=str(workspace.get("path", "")),
                before_hash=str(workspace.get("before_hash", "")),
                after_hash=str(workspace.get("after_hash", "")),
                files_before=int(workspace.get("files_before", 0) or 0),
                files_after=int(workspace.get("files_after", 0) or 0),
            ),
            error=str(payload.get("error", "")),
            path=path,
        )


@dataclass(frozen=True)
class ExperimentManifest:
    schema_version: int
    experiment_id: str
    created_at: str
    updated_at: str
    config_hash: str
    trial_count: int
    status_counts: dict[str, int]
    trial_ids: tuple[str, ...]
    trial_paths: tuple[str, ...]
    spec: ExperimentSpec
    path: Path | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "config_hash": self.config_hash,
            "trial_count": self.trial_count,
            "status_counts": dict(self.status_counts),
            "trial_ids": list(self.trial_ids),
            "trial_paths": list(self.trial_paths),
            "spec": self.spec.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: dict[str, Any], *, path: Path | None = None
    ) -> "ExperimentManifest":
        return cls(
            schema_version=int(payload.get("schema_version", 0) or 0),
            experiment_id=str(payload.get("experiment_id", "")),
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            config_hash=str(payload.get("config_hash", "")),
            trial_count=int(payload.get("trial_count", 0) or 0),
            status_counts={
                str(key): int(value)
                for key, value in dict(payload.get("status_counts") or {}).items()
            },
            trial_ids=tuple(str(item) for item in (payload.get("trial_ids") or ())),
            trial_paths=tuple(str(item) for item in (payload.get("trial_paths") or ())),
            spec=ExperimentSpec.from_dict(dict(payload.get("spec") or {})),
            path=path,
        )


@dataclass(frozen=True)
class ExperimentOutcome:
    manifest: ExperimentManifest
    records: tuple[RunRecord, ...]
    manifest_path: Path


class TrialExecutor(Protocol):
    async def execute(self, spec: ExperimentSpec, trial_index: int) -> TrialEvidence: ...


def compare_run_records(baseline: RunRecord, candidate: RunRecord) -> list[str]:
    """比较受控复跑的行为证据，忽略模型自然文本差异。"""
    diffs: list[str] = []
    if baseline.status != candidate.status:
        diffs.append("status")
    if baseline.task_hash != candidate.task_hash:
        diffs.append("task_hash")
    if baseline.config_hash != candidate.config_hash:
        diffs.append("config_hash")
    if baseline.model != candidate.model:
        diffs.append("model")
    if baseline.config != candidate.config:
        diffs.append("config")
    if list(baseline.verify_command) != list(candidate.verify_command):
        diffs.append("verify_command")
    if list(baseline.plan) != list(candidate.plan):
        diffs.append("plan")
    if list(baseline.node_path) != list(candidate.node_path):
        diffs.append("node_path")

    keys = ("critic", "guard_action", "guard_reason", "hitl_kind", "error_code", "outcome")
    for key in keys:
        if baseline.evidence.get(key) != candidate.evidence.get(key):
            diffs.append(key)
    if baseline.metrics.tool_calls != candidate.metrics.tool_calls:
        diffs.append("tool_calls")
    if baseline.metrics.tool_errors != candidate.metrics.tool_errors:
        diffs.append("tool_errors")
    if (
        baseline.workspace.before_hash
        and candidate.workspace.before_hash
        and baseline.workspace.before_hash != candidate.workspace.before_hash
    ):
        diffs.append("workspace.before_hash")
    return diffs


def with_experiment_id(spec: ExperimentSpec, experiment_id: str) -> ExperimentSpec:
    return replace(spec, experiment_id=experiment_id)


__all__ = [
    "ExperimentManifest",
    "ExperimentOutcome",
    "ExperimentSpec",
    "RunMetrics",
    "RunRecord",
    "SCHEMA_VERSION",
    "TrialEvidence",
    "TrialExecutor",
    "WorkspaceSnapshot",
    "compare_run_records",
    "fingerprint_workspace",
    "with_experiment_id",
]
