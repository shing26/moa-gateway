"""Experiment → Trial → Run Record 的执行外壳。"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime

from bench.experiment.store import ExperimentStore
from bench.experiment.types import (
    SCHEMA_VERSION,
    ExperimentManifest,
    ExperimentOutcome,
    ExperimentSpec,
    RunMetrics,
    RunRecord,
    TrialEvidence,
    TrialExecutor,
    WorkspaceSnapshot,
    fingerprint_workspace,
    with_experiment_id,
)

_TEXT_LIMIT = 8_000


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_experiment_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"exp-{stamp}-{uuid.uuid4().hex[:8]}"


def _task_hash(task: str) -> str:
    return hashlib.sha256(task.encode("utf-8")).hexdigest()


def _bounded(value: str) -> str:
    text = str(value or "")
    if len(text) <= _TEXT_LIMIT:
        return text
    return text[:_TEXT_LIMIT] + "\n…（Run Record 已截断）"


class ExperimentRunner:
    def __init__(
        self,
        store: ExperimentStore,
        *,
        text_limit: int = _TEXT_LIMIT,
    ) -> None:
        self._store = store
        self._text_limit = max(256, int(text_limit))

    async def run(
        self, spec: ExperimentSpec, executor: TrialExecutor
    ) -> ExperimentOutcome:
        experiment_id = spec.experiment_id or _new_experiment_id()
        spec = with_experiment_id(spec, experiment_id)
        manifest_path = self._store.manifest_path(experiment_id)
        if manifest_path.exists():
            raise FileExistsError(f"Experiment 已存在，拒绝覆盖: {manifest_path}")
        created_at = _now()
        records: list[RunRecord] = []
        status_counts: Counter[str] = Counter()
        trial_paths: list[str] = []
        manifest = ExperimentManifest(
            schema_version=SCHEMA_VERSION,
            experiment_id=experiment_id,
            created_at=created_at,
            updated_at=created_at,
            config_hash=spec.config_hash,
            trial_count=0,
            status_counts={},
            trial_ids=(),
            trial_paths=(),
            spec=spec,
        )
        self._store.write_manifest(manifest)

        for trial_index in range(spec.trials):
            started = time.monotonic()
            try:
                evidence = await executor.execute(spec, trial_index)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单 Trial 故障必须留证后继续
                evidence = TrialEvidence(
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                    workspace=self._fingerprint(spec.workspace_ref),
                )
            if not evidence.status:
                evidence = replace(evidence, status="error", error="Trial 未返回状态")

            trial_id = f"{experiment_id}-trial-{trial_index:03d}"
            record = RunRecord(
                schema_version=SCHEMA_VERSION,
                experiment_id=experiment_id,
                trial_id=trial_id,
                trial_index=trial_index,
                created_at=_now(),
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                task=spec.task,
                task_hash=_task_hash(spec.task),
                config_hash=spec.config_hash,
                adapter=spec.adapter,
                status=evidence.status,
                plan=evidence.plan,
                node_path=evidence.node_path,
                per_agent_outputs={
                    key: self._clip(value)
                    for key, value in evidence.per_agent_outputs.items()
                },
                final_text=self._clip(evidence.final_text),
                metrics=evidence.metrics,
                model=dict(spec.model),
                config=dict(spec.config),
                verify_command=spec.verify_command,
                prompt_hashes=evidence.prompt_hashes,
                evidence=evidence.evidence,
                workspace=evidence.workspace,
                error=self._clip(evidence.error),
            )
            path = self._store.write_record(record)
            record = replace(record, path=path)
            records.append(record)
            status_counts[record.status] += 1
            trial_paths.append(path.relative_to(self._store.experiment_dir(experiment_id)).as_posix())

            manifest = replace(
                manifest,
                updated_at=_now(),
                trial_count=len(records),
                status_counts=dict(sorted(status_counts.items())),
                trial_ids=tuple(item.trial_id for item in records),
                trial_paths=tuple(trial_paths),
            )
            self._store.write_manifest(manifest)

        manifest = replace(manifest, path=manifest_path)
        return ExperimentOutcome(
            manifest=manifest,
            records=tuple(records),
            manifest_path=manifest_path,
        )

    def _clip(self, value: str) -> str:
        text = str(value or "")
        if len(text) <= self._text_limit:
            return text
        return text[: self._text_limit] + "\n…（Run Record 已截断）"

    @staticmethod
    def _fingerprint(workspace_ref: str) -> WorkspaceSnapshot:
        return fingerprint_workspace(workspace_ref) if workspace_ref else WorkspaceSnapshot()


__all__ = ["ExperimentRunner"]
