"""可恢复的协议矩阵批次：Task × Arm 是 Experiment，Repeat 是 Trial。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench.agent.types import B1_TIER, B3_TIER
from bench.experiment.protocol import ExperimentProtocol, Phase
from bench.experiment.runner import ExperimentRunner
from bench.experiment.store import ExperimentStore
from bench.experiment.types import ExperimentManifest, ExperimentSpec, TrialExecutor
from bench.report import (
    DoseReport,
    ModelArmSummary,
    PairedComparison,
    RunRow,
    build_report,
    model_arm_matrix,
    paired_pass_rate_comparison,
    render_markdown,
    render_paired_markdown,
)

BATCH_SCHEMA_VERSION = 3
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_B3_TRIGGERS = frozenset({"fail", "budget_exhausted"})

ExecutorFactory = Callable[[ExperimentSpec], TrialExecutor]
BaselineByCell = Mapping[tuple[str, str], tuple[float, int]]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_batch_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"batch-{stamp}-{uuid.uuid4().hex[:8]}"


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"batch.{key} 必须是非空字符串")
    return value.strip()


def _integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"batch.{key} 必须是整数")
    return value


@dataclass(frozen=True)
class BatchCell:
    cell_id: str
    task_id: str
    task_origin: str
    arm: str
    experiment_id: str
    expected_trials: int
    status: str = "pending"
    trial_count: int = 0
    status_counts: dict[str, int] | None = None
    error: str = ""

    @property
    def complete(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "task_id": self.task_id,
            "task_origin": self.task_origin,
            "arm": self.arm,
            "experiment_id": self.experiment_id,
            "expected_trials": self.expected_trials,
            "status": self.status,
            "trial_count": self.trial_count,
            "status_counts": dict(self.status_counts or {}),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BatchCell":
        return cls(
            cell_id=_text(payload, "cell_id"),
            task_id=_text(payload, "task_id"),
            task_origin=_text(payload, "task_origin"),
            arm=_text(payload, "arm"),
            experiment_id=_text(payload, "experiment_id"),
            expected_trials=_integer(payload, "expected_trials"),
            status=_text(payload, "status"),
            trial_count=_integer(payload, "trial_count"),
            status_counts={
                str(key): int(value)
                for key, value in dict(payload.get("status_counts") or {}).items()
            },
            error=str(payload.get("error", "") or ""),
        )


@dataclass(frozen=True)
class BatchManifest:
    schema_version: int
    batch_id: str
    protocol_id: str
    protocol_version: str
    protocol_hash: str
    phase: str
    budget_tier: str
    source_batch_id: str
    created_at: str
    updated_at: str
    status: str
    resume_cursor: str
    cells: tuple[BatchCell, ...]
    max_concurrency: int = 1
    rate_limit_per_minute: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "protocol_id": self.protocol_id,
            "protocol_version": self.protocol_version,
            "protocol_hash": self.protocol_hash,
            "phase": self.phase,
            "budget_tier": self.budget_tier,
            "source_batch_id": self.source_batch_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "resume_cursor": self.resume_cursor,
            "cells": [cell.to_dict() for cell in self.cells],
            "max_concurrency": self.max_concurrency,
            "rate_limit_per_minute": self.rate_limit_per_minute,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BatchManifest":
        raw_cells = payload.get("cells")
        if not isinstance(raw_cells, list):
            raise TypeError("batch.cells 必须是数组")
        cells = tuple(
            BatchCell.from_dict(item)
            for item in raw_cells
            if isinstance(item, dict)
        )
        if len(cells) != len(raw_cells):
            raise TypeError("batch.cells 的每一项都必须是对象")
        schema_version = _integer(payload, "schema_version")
        if schema_version not in (1, 2, BATCH_SCHEMA_VERSION):
            raise ValueError(
                f"不支持的 batch.schema_version: {schema_version}"
                f"（支持 1、2 与 {BATCH_SCHEMA_VERSION}）"
            )
        budget_tier = str(payload.get("budget_tier", B1_TIER) or B1_TIER)
        if budget_tier not in (B1_TIER, B3_TIER):
            raise ValueError(f"batch.budget_tier 非法: {budget_tier!r}")
        max_concurrency = int(payload.get("max_concurrency", 1) or 1)
        rate_limit_per_minute = int(
            payload.get("rate_limit_per_minute", 0) or 0
        )
        if max_concurrency < 1:
            raise ValueError("batch.max_concurrency 必须 >= 1")
        if rate_limit_per_minute < 0:
            raise ValueError("batch.rate_limit_per_minute 不能为负数")
        return cls(
            schema_version=schema_version,
            batch_id=_text(payload, "batch_id"),
            protocol_id=_text(payload, "protocol_id"),
            protocol_version=_text(payload, "protocol_version"),
            protocol_hash=_text(payload, "protocol_hash"),
            phase=_text(payload, "phase"),
            budget_tier=budget_tier,
            source_batch_id=str(payload.get("source_batch_id", "") or ""),
            created_at=_text(payload, "created_at"),
            updated_at=_text(payload, "updated_at"),
            status=_text(payload, "status"),
            resume_cursor=str(payload.get("resume_cursor", "") or ""),
            cells=cells,
            max_concurrency=max_concurrency,
            rate_limit_per_minute=rate_limit_per_minute,
        )


class BatchStore:
    def __init__(self, experiment_root: Path | str) -> None:
        self.root = Path(experiment_root).resolve()
        self.batches_root = self.root / "_batches"

    def batch_dir(self, batch_id: str) -> Path:
        if not _SAFE_ID.fullmatch(batch_id):
            raise ValueError(f"非法 batch_id: {batch_id!r}")
        return self.batches_root / batch_id

    def manifest_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "batch.json"

    def report_json_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "report.json"

    def report_markdown_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "report.md"

    def write(self, manifest: BatchManifest) -> Path:
        path = self.manifest_path(manifest.batch_id)
        self._atomic_write(path, manifest.to_dict())
        return path

    def load(self, batch_id: str) -> BatchManifest:
        path = self.manifest_path(batch_id)
        return BatchManifest.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def write_report(self, report: "BatchReport") -> tuple[Path, Path]:
        batch_id = report.manifest.batch_id
        json_path = self.report_json_path(batch_id)
        markdown_path = self.report_markdown_path(batch_id)
        self._atomic_write(json_path, report.to_dict())
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_text(markdown_path, report.render_markdown())
        return json_path, markdown_path

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    @staticmethod
    def _atomic_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(text.rstrip() + "\n", encoding="utf-8")
        try:
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()


@dataclass(frozen=True)
class BatchReport:
    manifest: BatchManifest
    dose: DoseReport
    paired: PairedComparison | None = None
    model_summaries: tuple[ModelArmSummary, ...] = ()

    @property
    def expected_runs(self) -> int:
        return sum(cell.expected_trials for cell in self.manifest.cells)

    @property
    def completed_runs(self) -> int:
        return sum(cell.trial_count for cell in self.manifest.cells)

    @property
    def missing_runs(self) -> int:
        return max(0, self.expected_runs - self.completed_runs)

    @property
    def analysis_authorized(self) -> bool:
        return bool(self.paired is not None and self.paired.authorized)

    @property
    def statistical_claims(self) -> bool:
        return bool(
            self.paired is not None
            and self.paired.significant_difference
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "batch_id": self.manifest.batch_id,
            "protocol_id": self.manifest.protocol_id,
            "protocol_version": self.manifest.protocol_version,
            "protocol_hash": self.manifest.protocol_hash,
            "phase": self.manifest.phase,
            "budget_tier": self.manifest.budget_tier,
            "source_batch_id": self.manifest.source_batch_id,
            "status": self.manifest.status,
            "resume_cursor": self.manifest.resume_cursor,
            "max_concurrency": self.manifest.max_concurrency,
            "rate_limit_per_minute": self.manifest.rate_limit_per_minute,
            "cell_count": len(self.manifest.cells),
            "completed_cells": sum(
                1 for cell in self.manifest.cells if cell.complete
            ),
            "expected_runs": self.expected_runs,
            "completed_runs": self.completed_runs,
            "missing_runs": self.missing_runs,
            "analysis_authorized": self.analysis_authorized,
            "statistical_claims": self.statistical_claims,
            "cells": [cell.to_dict() for cell in self.manifest.cells],
            "report": self.dose.to_dict(),
        }
        if self.paired is not None:
            payload["statistics"] = self.paired.to_dict()
            payload["model_matrix"] = [
                item.to_dict() for item in self.model_summaries
            ]
        return payload

    def render_markdown(self) -> str:
        lines = [
            "# 多智能体实验批次报告",
            "",
            f"- 批次：`{self.manifest.batch_id}`",
            f"- 协议：`{self.manifest.protocol_id}` v{self.manifest.protocol_version}",
            f"- 协议哈希：`{self.manifest.protocol_hash}`",
            f"- 阶段：`{self.manifest.phase}`",
            f"- 预算档：`{self.manifest.budget_tier}`",
            f"- 并发上限：`{self.manifest.max_concurrency}`",
            (
                "- Trial 启动速率："
                f"`{self.manifest.rate_limit_per_minute or 'unlimited'}`"
            ),
            f"- 状态：`{self.manifest.status}`",
            (
                f"- 单元：{sum(1 for cell in self.manifest.cells if cell.complete)}"
                f" / {len(self.manifest.cells)} 完成"
            ),
            (
                f"- Trial：{self.completed_runs} / {self.expected_runs} 完成，"
                f"{self.missing_runs} 缺失"
            ),
            (
                "- 分析门禁：**已按预注册门禁授权**。"
                if self.analysis_authorized
                else "- 分析门禁：**不授权**；样本不足时只作方向性观察。"
            ),
            (
                "- 显著差异声明：**已授权**。"
                if self.statistical_claims
                else "- 显著差异声明：**不授权**；不得宣称 arm 更优或更差。"
            ),
            "",
            "## 单元状态",
        ]
        if self.manifest.source_batch_id:
            lines.insert(6, f"- B1 来源批次：`{self.manifest.source_batch_id}`")
        lines.extend(
            [
                "",
                "| 任务 | Arm | 状态 | Trials | 结局 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for cell in self.manifest.cells:
            outcomes = ", ".join(
                f"{key}={value}"
                for key, value in sorted((cell.status_counts or {}).items())
            )
            lines.append(
                f"| {cell.task_id} | {cell.arm} | {cell.status} | "
                f"{cell.trial_count}/{cell.expected_trials} | {outcomes or '-'} |"
            )
        lines.extend(["", render_markdown(self.dose)])
        if self.paired is not None:
            lines.extend(["", render_paired_markdown(self.paired)])
        return "\n".join(lines)


@dataclass(frozen=True)
class BatchOutcome:
    manifest: BatchManifest
    report: BatchReport
    manifest_path: Path
    report_json_path: Path
    report_markdown_path: Path


class _TrialStartPacer:
    def __init__(self, rate_limit_per_minute: int) -> None:
        self._interval_s = (
            60.0 / rate_limit_per_minute if rate_limit_per_minute > 0 else 0.0
        )
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def wait(self) -> None:
        if self._interval_s <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            scheduled = max(now, self._next_start)
            self._next_start = scheduled + self._interval_s
        delay = scheduled - now
        if delay > 0:
            await asyncio.sleep(delay)


class _LimitedTrialExecutor:
    def __init__(
        self,
        executor: TrialExecutor,
        gate: asyncio.Semaphore,
        pacer: _TrialStartPacer,
        on_start: Callable[[], Awaitable[None]],
        on_finish: Callable[[], Awaitable[None]],
    ) -> None:
        self._executor = executor
        self._gate = gate
        self._pacer = pacer
        self._on_start = on_start
        self._on_finish = on_finish

    async def execute(
        self, spec: ExperimentSpec, trial_index: int
    ) -> Any:
        async with self._gate:
            await self._pacer.wait()
            await self._on_start()
            try:
                return await self._executor.execute(spec, trial_index)
            finally:
                await self._on_finish()


class BatchRunner:
    def __init__(
        self,
        experiment_store: ExperimentStore,
        protocol: ExperimentProtocol,
    ) -> None:
        self._experiments = experiment_store
        self._protocol = protocol
        self._batches = BatchStore(experiment_store.root)

    async def run(
        self,
        *,
        phase: Phase,
        executor_factory: ExecutorFactory,
        batch_id: str = "",
        task_ids: tuple[str, ...] | None = None,
        arm_names: tuple[str, ...] | None = None,
        budget_tier: str = B1_TIER,
        source_batch_id: str = "",
        baseline_by_cell: BaselineByCell | None = None,
        cell_keys: set[tuple[str, str]] | None = None,
        max_concurrency: int = 1,
        rate_limit_per_minute: int = 0,
    ) -> BatchOutcome:
        if budget_tier not in (B1_TIER, B3_TIER):
            raise ValueError(f"未知预算档位: {budget_tier!r}")
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        if rate_limit_per_minute < 0:
            raise ValueError("rate_limit_per_minute 不能为负数")
        selected_tasks = self._select_tasks(task_ids)
        selected_arms = self._select_arms(arm_names)
        batch_id = batch_id.strip() or _new_batch_id()
        cells = self._expected_cells(
            phase=phase,
            batch_id=batch_id,
            task_ids=selected_tasks,
            arm_names=selected_arms,
            budget_tier=budget_tier,
            source_batch_id=source_batch_id,
            baseline_by_cell=baseline_by_cell or {},
            cell_keys=cell_keys,
        )
        manifest_path = self._batches.manifest_path(batch_id)
        if manifest_path.exists():
            manifest = self._load_compatible(
                batch_id=batch_id,
                phase=phase,
                expected_cells=cells,
                budget_tier=budget_tier,
                source_batch_id=source_batch_id,
                max_concurrency=max_concurrency,
                rate_limit_per_minute=rate_limit_per_minute,
            )
        else:
            created = _now()
            manifest = BatchManifest(
                schema_version=BATCH_SCHEMA_VERSION,
                batch_id=batch_id,
                protocol_id=self._protocol.protocol_id,
                protocol_version=self._protocol.version,
                protocol_hash=self._protocol.config_hash,
                phase=phase,
                budget_tier=budget_tier,
                source_batch_id=source_batch_id.strip(),
                created_at=created,
                updated_at=created,
                status="running",
                resume_cursor=cells[0].cell_id,
                cells=cells,
                max_concurrency=max_concurrency,
                rate_limit_per_minute=rate_limit_per_minute,
            )
            self._batches.write(manifest)

        manifest = replace(
            manifest,
            updated_at=_now(),
            status="running",
            resume_cursor=self._resume_cursor(manifest.cells),
        )
        self._batches.write(manifest)

        for index, cell in enumerate(manifest.cells):
            if cell.status != "completed":
                continue
            spec = self._protocol.experiment_spec(
                cell.task_id,
                cell.arm,
                phase=phase,
                batch_id=batch_id,
                budget_tier=budget_tier,
                source_batch_id=source_batch_id,
                baseline_cost_usd=(baseline_by_cell or {})
                .get((cell.task_id, cell.arm), (0.0, 0))[0],
                baseline_tokens=(baseline_by_cell or {})
                .get((cell.task_id, cell.arm), (0.0, 0))[1],
            )
            if spec.experiment_id != cell.experiment_id:
                raise ValueError("批次单元与协议映射出的 Experiment ID 不一致")
            if not self._evidence_complete(spec):
                cell = replace(
                    cell,
                    status="error",
                    error="Batch 标记完成，但 Experiment/Run Record 证据不完整",
                )
                manifest = self._replace_cell(manifest, index, cell)
                self._batches.write(manifest)

        gate = asyncio.Semaphore(max_concurrency)
        pacer = _TrialStartPacer(rate_limit_per_minute)
        manifest_lock = asyncio.Lock()

        async def run_cell(index: int, initial_cell: BatchCell) -> None:
            nonlocal manifest
            spec = self._protocol.experiment_spec(
                initial_cell.task_id,
                initial_cell.arm,
                phase=phase,
                batch_id=batch_id,
                budget_tier=budget_tier,
                source_batch_id=source_batch_id,
                baseline_cost_usd=(baseline_by_cell or {})
                .get((initial_cell.task_id, initial_cell.arm), (0.0, 0))[0],
                baseline_tokens=(baseline_by_cell or {})
                .get((initial_cell.task_id, initial_cell.arm), (0.0, 0))[1],
            )
            if spec.experiment_id != initial_cell.experiment_id:
                raise ValueError("批次单元与协议映射出的 Experiment ID 不一致")

            async def mark_running() -> None:
                nonlocal manifest
                async with manifest_lock:
                    current = manifest.cells[index]
                    running = replace(
                        self._with_experiment_progress(current, spec),
                        status="running",
                        error="",
                    )
                    manifest = self._replace_cell(manifest, index, running)
                    manifest = replace(
                        manifest,
                        updated_at=_now(),
                        resume_cursor=running.cell_id,
                    )
                    self._batches.write(manifest)

            async def mark_idle() -> None:
                nonlocal manifest
                async with manifest_lock:
                    current = manifest.cells[index]
                    if current.status == "completed":
                        return
                    idle = replace(
                        self._with_experiment_progress(current, spec),
                        status="pending",
                    )
                    manifest = self._replace_cell(manifest, index, idle)
                    manifest = replace(
                        manifest,
                        updated_at=_now(),
                        resume_cursor=self._resume_cursor(manifest.cells),
                    )
                    self._batches.write(manifest)

            async def update_progress(
                experiment_manifest: ExperimentManifest,
            ) -> None:
                nonlocal manifest
                async with manifest_lock:
                    current = manifest.cells[index]
                    updated = replace(
                        current,
                        trial_count=experiment_manifest.trial_count,
                        status_counts=dict(experiment_manifest.status_counts),
                        error="",
                    )
                    manifest = self._replace_cell(manifest, index, updated)
                    manifest = replace(
                        manifest,
                        updated_at=_now(),
                        resume_cursor=self._resume_cursor(manifest.cells),
                    )
                    self._batches.write(manifest)

            try:
                executor = executor_factory(spec)
                outcome = await ExperimentRunner(
                    self._experiments
                ).run_or_resume(
                    spec,
                    _LimitedTrialExecutor(
                        executor,
                        gate,
                        pacer,
                        on_start=mark_running,
                        on_finish=mark_idle,
                    ),
                    on_trial_complete=update_progress,
                )
            except asyncio.CancelledError:
                current = manifest.cells[index]
                paused = replace(
                    self._with_experiment_progress(current, spec),
                    status="pending",
                    error="CancelledError: batch interrupted",
                )
                async with manifest_lock:
                    manifest = self._replace_cell(manifest, index, paused)
                    manifest = replace(
                        manifest,
                        updated_at=_now(),
                        status="paused",
                        resume_cursor=paused.cell_id,
                    )
                    self._batches.write(manifest)
                raise
            except Exception as exc:  # noqa: BLE001 - 单单元故障不能吞掉后续单元
                current = manifest.cells[index]
                failed = replace(
                    self._with_experiment_progress(current, spec),
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                async with manifest_lock:
                    manifest = self._replace_cell(manifest, index, failed)
            else:
                async with manifest_lock:
                    complete = replace(
                        manifest.cells[index],
                        status="completed",
                        trial_count=outcome.manifest.trial_count,
                        status_counts=dict(outcome.manifest.status_counts),
                        error="",
                    )
                    manifest = self._replace_cell(manifest, index, complete)

            async with manifest_lock:
                manifest = replace(
                    manifest,
                    updated_at=_now(),
                    resume_cursor=self._resume_cursor(manifest.cells),
                )
                self._batches.write(manifest)

        cells_to_run = [
            (index, cell)
            for index, cell in enumerate(manifest.cells)
            if cell.status != "completed"
        ]
        tasks = [
            asyncio.create_task(run_cell(index, cell))
            for index, cell in cells_to_run
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            async with manifest_lock:
                manifest = replace(
                    manifest,
                    updated_at=_now(),
                    status="paused",
                    resume_cursor=self._resume_cursor(manifest.cells),
                )
                self._batches.write(manifest)
            raise

        all_complete = all(cell.complete for cell in manifest.cells)
        status = (
            "completed"
            if all_complete
            else "completed_with_errors"
            if any(cell.status == "error" for cell in manifest.cells)
            else "running"
        )
        manifest = replace(
            manifest,
            updated_at=_now(),
            status=status,
            resume_cursor=self._resume_cursor(manifest.cells),
        )
        self._batches.write(manifest)
        report = build_batch_report(manifest, self._experiments, self._protocol)
        report_json, report_markdown = self._batches.write_report(report)
        return BatchOutcome(
            manifest=manifest,
            report=report,
            manifest_path=self._batches.manifest_path(batch_id),
            report_json_path=report_json,
            report_markdown_path=report_markdown,
        )

    async def run_b3(
        self,
        *,
        source_batch_id: str,
        executor_factory: ExecutorFactory,
        phase: Phase = "formal",
        batch_id: str = "",
        max_concurrency: int | None = None,
        rate_limit_per_minute: int | None = None,
    ) -> BatchOutcome:
        """从完整 B1 批次派生 B3：只复跑 fail / budget_exhausted 单元。"""
        source = self._batches.load(source_batch_id)
        if source.protocol_hash != self._protocol.config_hash:
            raise ValueError(
                "B1 来源批次的 protocol_hash 与当前协议不一致，拒绝生成 B3: "
                f"{source.protocol_hash} != {self._protocol.config_hash}"
            )
        if source.budget_tier != B1_TIER:
            raise ValueError("B3 来源批次必须是 B1")
        incomplete = tuple(cell.cell_id for cell in source.cells if not cell.complete)
        if incomplete:
            raise ValueError(
                "B1 来源批次仍有未完成单元，拒绝生成 B3: "
                + ", ".join(incomplete)
            )

        selected: list[tuple[str, str]] = []
        baselines: dict[tuple[str, str], tuple[float, int]] = {}
        for cell in source.cells:
            experiment = self._experiments.load_manifest(cell.experiment_id)
            source_tier = str(
                experiment.spec.config.get("budget_tier", B1_TIER) or B1_TIER
            )
            if source_tier != B1_TIER:
                raise ValueError(
                    f"来源单元不是 B1: {cell.cell_id} ({source_tier})"
                )
            records = self._experiments.load_experiment_records(cell.experiment_id)
            if not records:
                raise ValueError(f"来源单元没有 Run Record: {cell.cell_id}")
            if not any(record.status in _B3_TRIGGERS for record in records):
                continue
            key = (cell.task_id, cell.arm)
            selected.append(key)
            baselines[key] = (
                sum(float(record.metrics.cost_usd) for record in records),
                sum(int(record.metrics.total_tokens) for record in records),
            )

        if not selected:
            raise ValueError("B1 批次没有 B3 候选（无 fail / budget_exhausted）")

        task_ids = tuple(dict.fromkeys(task_id for task_id, _ in selected))
        arm_names = tuple(dict.fromkeys(arm for _, arm in selected))
        return await self.run(
            phase=phase,
            batch_id=batch_id,
            task_ids=task_ids,
            arm_names=arm_names,
            budget_tier=B3_TIER,
            source_batch_id=source_batch_id,
            baseline_by_cell=baselines,
            cell_keys=set(selected),
            executor_factory=executor_factory,
            max_concurrency=(
                source.max_concurrency
                if max_concurrency is None
                else max_concurrency
            ),
            rate_limit_per_minute=(
                source.rate_limit_per_minute
                if rate_limit_per_minute is None
                else rate_limit_per_minute
            ),
        )

    def _select_tasks(self, task_ids: tuple[str, ...] | None) -> tuple[str, ...]:
        if task_ids is None:
            return tuple(task.task_id for task in self._protocol.tasks)
        declared = {task.task_id for task in self._protocol.tasks}
        unknown = tuple(task_id for task_id in task_ids if task_id not in declared)
        if unknown:
            raise ValueError(f"协议不包含 task_id: {', '.join(unknown)}")
        chosen = set(task_ids)
        return tuple(task.task_id for task in self._protocol.tasks if task.task_id in chosen)

    def _select_arms(self, arm_names: tuple[str, ...] | None) -> tuple[str, ...]:
        if arm_names is None:
            return tuple(arm.name for arm in self._protocol.arms)
        declared = {arm.name for arm in self._protocol.arms}
        unknown = tuple(name for name in arm_names if name not in declared)
        if unknown:
            raise ValueError(f"协议不包含 arm: {', '.join(unknown)}")
        chosen = set(arm_names)
        return tuple(arm.name for arm in self._protocol.arms if arm.name in chosen)

    def _expected_cells(
        self,
        *,
        phase: Phase,
        batch_id: str,
        task_ids: tuple[str, ...],
        arm_names: tuple[str, ...],
        budget_tier: str,
        source_batch_id: str,
        baseline_by_cell: BaselineByCell,
        cell_keys: set[tuple[str, str]] | None,
    ) -> tuple[BatchCell, ...]:
        cells: list[BatchCell] = []
        for task in self._protocol.tasks:
            if task.task_id not in task_ids:
                continue
            for arm in self._protocol.arms:
                if arm.name not in arm_names:
                    continue
                if cell_keys is not None and (task.task_id, arm.name) not in cell_keys:
                    continue
                baseline_cost, baseline_tokens = baseline_by_cell.get(
                    (task.task_id, arm.name), (0.0, 0)
                )
                spec = self._protocol.experiment_spec(
                    task.task_id,
                    arm.name,
                    phase=phase,
                    batch_id=batch_id,
                    budget_tier=budget_tier,
                    source_batch_id=source_batch_id,
                    baseline_cost_usd=baseline_cost,
                    baseline_tokens=baseline_tokens,
                )
                cells.append(
                    BatchCell(
                        cell_id=f"{task.task_id}__{arm.name}",
                        task_id=task.task_id,
                        task_origin=task.origin,
                        arm=arm.name,
                        experiment_id=spec.experiment_id,
                        expected_trials=spec.trials,
                    )
                )
        if not cells:
            raise ValueError("批次没有可执行单元")
        return tuple(cells)

    def _load_compatible(
        self,
        *,
        batch_id: str,
        phase: str,
        expected_cells: tuple[BatchCell, ...],
        budget_tier: str,
        source_batch_id: str,
        max_concurrency: int,
        rate_limit_per_minute: int,
    ) -> BatchManifest:
        manifest = self._batches.load(batch_id)
        if manifest.protocol_hash != self._protocol.config_hash:
            raise ValueError(
                "Batch 的 protocol_hash 与当前协议不一致，拒绝恢复: "
                f"{manifest.protocol_hash} != {self._protocol.config_hash}"
            )
        if manifest.phase != phase:
            raise ValueError(
                f"Batch 阶段不一致，拒绝恢复: {manifest.phase!r} != {phase!r}"
            )
        if manifest.budget_tier != budget_tier:
            raise ValueError(
                "Batch 预算档不一致，拒绝恢复: "
                f"{manifest.budget_tier!r} != {budget_tier!r}"
            )
        if manifest.source_batch_id != source_batch_id.strip():
            raise ValueError(
                "Batch B3 来源不一致，拒绝恢复: "
                f"{manifest.source_batch_id!r} != {source_batch_id.strip()!r}"
            )
        if manifest.max_concurrency != max_concurrency:
            raise ValueError(
                "Batch 并发上限不一致，拒绝恢复: "
                f"{manifest.max_concurrency} != {max_concurrency}"
            )
        if manifest.rate_limit_per_minute != rate_limit_per_minute:
            raise ValueError(
                "Batch Trial 启动速率不一致，拒绝恢复: "
                f"{manifest.rate_limit_per_minute} != {rate_limit_per_minute}"
            )
        expected_signature = [
            (
                cell.cell_id,
                cell.task_id,
                cell.arm,
                cell.experiment_id,
                cell.expected_trials,
            )
            for cell in expected_cells
        ]
        actual_signature = [
            (
                cell.cell_id,
                cell.task_id,
                cell.arm,
                cell.experiment_id,
                cell.expected_trials,
            )
            for cell in manifest.cells
        ]
        if actual_signature != expected_signature:
            raise ValueError("Batch 单元矩阵与当前协议选择不一致，拒绝恢复")
        return manifest

    def _evidence_complete(self, spec: ExperimentSpec) -> bool:
        if not self._experiments.manifest_path(spec.experiment_id).exists():
            return False
        manifest = self._experiments.load_manifest(spec.experiment_id)
        if manifest.config_hash != spec.config_hash:
            return False
        if manifest.trial_count != spec.trials:
            return False
        records = self._experiments.load_experiment_records(spec.experiment_id)
        return len(records) == spec.trials and all(
            record.trial_index == index for index, record in enumerate(records)
        )

    def _with_experiment_progress(
        self, cell: BatchCell, spec: ExperimentSpec
    ) -> BatchCell:
        if not self._experiments.manifest_path(spec.experiment_id).exists():
            return cell
        manifest = self._experiments.load_manifest(spec.experiment_id)
        return replace(
            cell,
            trial_count=manifest.trial_count,
            status_counts=dict(manifest.status_counts),
        )

    @staticmethod
    def _replace_cell(
        manifest: BatchManifest, index: int, cell: BatchCell
    ) -> BatchManifest:
        cells = list(manifest.cells)
        cells[index] = cell
        return replace(manifest, cells=tuple(cells))

    @staticmethod
    def _resume_cursor(cells: tuple[BatchCell, ...]) -> str:
        return next((cell.cell_id for cell in cells if not cell.complete), "")


def build_batch_report(
    manifest: BatchManifest,
    experiment_store: ExperimentStore,
    protocol: ExperimentProtocol | None = None,
) -> BatchReport:
    rows = load_batch_rows(manifest, experiment_store)
    dose = build_report(
        rows,
        tier=manifest.budget_tier,
        tasks=(cell.task_id for cell in manifest.cells),
        arms=tuple(dict.fromkeys(cell.arm for cell in manifest.cells)),
    )
    paired = None
    if protocol is not None and protocol.statistics is not None:
        paired = paired_pass_rate_comparison(
            rows,
            protocol.statistics,
            expected_tasks=(task.task_id for task in protocol.tasks),
            expected_task_origins={
                task.task_id: task.origin for task in protocol.tasks
            },
        )
    return BatchReport(
        manifest=manifest,
        dose=dose,
        paired=paired,
        model_summaries=model_arm_matrix(rows) if paired is not None else (),
    )


def load_batch_rows(
    manifest: BatchManifest,
    experiment_store: ExperimentStore,
) -> list[RunRow]:
    origins = {cell.task_id: cell.task_origin for cell in manifest.cells}
    rows: list[RunRow] = []
    model_identities: set[str] = set()
    for cell in manifest.cells:
        if cell.status != "completed":
            continue
        experiment = experiment_store.load_manifest(cell.experiment_id)
        tier = str(
            experiment.spec.config.get("budget_tier", B1_TIER) or B1_TIER
        )
        if tier != manifest.budget_tier:
            raise ValueError(
                f"{cell.cell_id} 的 Experiment 预算档 {tier!r} "
                f"与 Batch {manifest.budget_tier!r} 不一致"
            )
        baseline_cost = float(
            experiment.spec.config.get("baseline_cost_usd", 0.0) or 0.0
        )
        baseline_tokens = int(
            experiment.spec.config.get("baseline_tokens", 0) or 0
        )
        for record in experiment_store.load_experiment_records(cell.experiment_id):
            model_identity = _model_identity(record.model)
            model_identities.add(model_identity)
            rows.append(
                RunRow(
                    task_id=cell.task_id,
                    arm=cell.arm,
                    outcome=record.status,
                    cost_usd=record.metrics.cost_usd,
                    prompt_tokens=record.metrics.prompt_tokens,
                    completion_tokens=record.metrics.completion_tokens,
                    budget_tier=tier,
                    model=str(record.model.get("model", "") or ""),
                    model_config=model_identity,
                    wall_clock_s=record.duration_ms / 1000,
                    source=str(record.path or ""),
                    task_origin=origins.get(cell.task_id, "E1"),  # type: ignore[arg-type]
                    baseline_cost_usd=baseline_cost,
                    baseline_tokens=baseline_tokens,
                )
            )
    if len(model_identities) > 1:
        raise ValueError(
            f"批次 {manifest.batch_id} 包含多个模型配置: "
            f"{sorted(model_identities)}"
        )
    return rows


def _model_identity(model: Mapping[str, Any]) -> str:
    name = str(model.get("model", "") or "")
    if not name:
        return ""
    parts = [name]
    for key in ("temperature", "max_tokens"):
        if key in model:
            parts.append(f"{key}={model[key]}")
    return "|".join(str(item) for item in parts)


__all__ = [
    "BATCH_SCHEMA_VERSION",
    "BatchCell",
    "BatchManifest",
    "BatchOutcome",
    "BatchReport",
    "BatchRunner",
    "BatchStore",
    "BaselineByCell",
    "ExecutorFactory",
    "build_batch_report",
    "load_batch_rows",
]
