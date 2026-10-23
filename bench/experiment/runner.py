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


def _retry_policy(spec: ExperimentSpec) -> tuple[int, frozenset[str]]:
    payload = spec.metadata.get("retry") or {}
    if not isinstance(payload, dict):
        raise TypeError("Experiment.metadata.retry 必须是对象")
    retries = int(payload.get("max_error_retries", 0) or 0)
    if retries < 0:
        raise ValueError("Experiment.metadata.retry.max_error_retries 不能为负数")
    retry_on = frozenset(str(item) for item in (payload.get("retry_on") or ()))
    do_not_retry_on = frozenset(
        str(item) for item in (payload.get("do_not_retry_on") or ())
    )
    overlap = retry_on & do_not_retry_on
    if overlap:
        raise ValueError(
            "Experiment.metadata.retry.retry_on 与 do_not_retry_on 重叠: "
            f"{sorted(overlap)}"
        )
    return retries, retry_on


def _sum_metrics(items: list[RunMetrics]) -> RunMetrics:
    return RunMetrics(
        llm_calls=sum(item.llm_calls for item in items),
        prompt_tokens=sum(item.prompt_tokens for item in items),
        completion_tokens=sum(item.completion_tokens for item in items),
        cost_usd=sum(item.cost_usd for item in items),
        llm_latency_ms=sum(item.llm_latency_ms for item in items),
        tool_calls=sum(item.tool_calls for item in items),
        tool_errors=sum(item.tool_errors for item in items),
        tool_arg_rejections=sum(item.tool_arg_rejections for item in items),
    )


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
        return await self._execute_pending(spec, executor, manifest)

    async def resume(
        self, spec: ExperimentSpec, executor: TrialExecutor
    ) -> ExperimentOutcome:
        """从已落盘的 Experiment 继续缺失 Trial；绝不覆盖已有 Run Record。"""
        if not spec.experiment_id:
            raise ValueError("resume 需要显式 experiment_id")
        manifest_path = self._store.manifest_path(spec.experiment_id)
        if not manifest_path.exists():
            raise FileNotFoundError(f"Experiment manifest 不存在: {manifest_path}")
        manifest = self._store.load_manifest(spec.experiment_id)
        if manifest.config_hash != spec.config_hash:
            raise ValueError(
                "Experiment 配置与已有 manifest 不一致，拒绝恢复: "
                f"{manifest.config_hash} != {spec.config_hash}"
            )
        if manifest.spec.trials != spec.trials:
            raise ValueError(
                "Experiment Trial 数与已有 manifest 不一致，拒绝恢复: "
                f"{manifest.spec.trials} != {spec.trials}"
            )
        records = list(self._store.load_experiment_records(spec.experiment_id))
        for index, record in enumerate(records):
            if record.trial_index != index:
                raise ValueError(
                    f"Run Record Trial 序号不连续: 位置 {index} 是 {record.trial_index}"
                )
            if record.config_hash != spec.config_hash:
                raise ValueError(
                    f"Run Record 配置哈希不一致，拒绝恢复: {record.trial_id}"
                )

        # 进程可能在 Run Record 已落盘、Experiment manifest 尚未更新时中断。
        # 只恢复连续尾部，不跨过缺口猜哪条记录算完成。
        next_index = len(records)
        while next_index < spec.trials:
            path = self._store.trial_path(spec.experiment_id, next_index)
            if not path.exists():
                break
            record = self._store.load_record(path)
            if record.trial_index != next_index:
                raise ValueError(f"Run Record Trial 序号不连续: {path}")
            if record.config_hash != spec.config_hash:
                raise ValueError(f"Run Record 配置哈希不一致，拒绝恢复: {path}")
            records.append(record)
            next_index += 1

        status_counts: Counter[str] = Counter(record.status for record in records)
        manifest = replace(
            manifest,
            updated_at=_now(),
            trial_count=len(records),
            status_counts=dict(sorted(status_counts.items())),
            trial_ids=tuple(item.trial_id for item in records),
            trial_paths=tuple(
                item.path.relative_to(
                    self._store.experiment_dir(spec.experiment_id)
                ).as_posix()
                for item in records
                if item.path is not None
            ),
        )
        self._store.write_manifest(manifest)
        return await self._execute_pending(spec, executor, manifest)

    async def run_or_resume(
        self, spec: ExperimentSpec, executor: TrialExecutor
    ) -> ExperimentOutcome:
        if spec.experiment_id and self._store.manifest_path(spec.experiment_id).exists():
            return await self.resume(spec, executor)
        return await self.run(spec, executor)

    async def _execute_pending(
        self,
        spec: ExperimentSpec,
        executor: TrialExecutor,
        manifest: ExperimentManifest,
    ) -> ExperimentOutcome:
        records = list(
            self._store.load_experiment_records(spec.experiment_id)
            if manifest.trial_count
            else ()
        )
        status_counts: Counter[str] = Counter(record.status for record in records)
        trial_paths = [
            record.path.relative_to(
                self._store.experiment_dir(spec.experiment_id)
            ).as_posix()
            for record in records
            if record.path is not None
        ]

        for trial_index in range(manifest.trial_count, spec.trials):
            retry_limit, retry_on = _retry_policy(spec)
            attempts: list[TrialEvidence] = []
            attempt_durations: list[float] = []
            for attempt_index in range(retry_limit + 1):
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
                    evidence = replace(
                        evidence,
                        status="error",
                        error="Trial 未返回状态",
                    )
                attempt_durations.append(
                    round((time.monotonic() - started) * 1000, 1)
                )
                attempts.append(evidence)
                if evidence.status not in retry_on or attempt_index >= retry_limit:
                    break

            evidence = attempts[-1]
            if len(attempts) > 1:
                evidence = replace(
                    evidence,
                    metrics=_sum_metrics([item.metrics for item in attempts]),
                    prompt_hashes=tuple(
                        prompt_hash
                        for item in attempts
                        for prompt_hash in item.prompt_hashes
                    ),
                    evidence={
                        **evidence.evidence,
                        "retry_attempts": [
                            {
                                "attempt_index": index,
                                "status": item.status,
                                "error": item.error,
                                "duration_ms": attempt_durations[index],
                                "metrics": item.metrics.to_dict(),
                                "evidence": item.evidence,
                                "workspace": item.workspace.to_dict(),
                            }
                            for index, item in enumerate(attempts)
                        ],
                    },
                )

            trial_id = f"{spec.experiment_id}-trial-{trial_index:03d}"
            record = RunRecord(
                schema_version=SCHEMA_VERSION,
                experiment_id=spec.experiment_id,
                trial_id=trial_id,
                trial_index=trial_index,
                created_at=_now(),
                duration_ms=round(sum(attempt_durations), 1),
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
            trial_paths.append(
                path.relative_to(
                    self._store.experiment_dir(spec.experiment_id)
                ).as_posix()
            )

            manifest = replace(
                manifest,
                updated_at=_now(),
                trial_count=len(records),
                status_counts=dict(sorted(status_counts.items())),
                trial_ids=tuple(item.trial_id for item in records),
                trial_paths=tuple(trial_paths),
            )
            self._store.write_manifest(manifest)

        manifest_path = self._store.manifest_path(spec.experiment_id)
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
