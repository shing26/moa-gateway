"""Cross-model comparison over completed, independently executed batches."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench.experiment.batch import (
    BatchManifest,
    BatchStore,
    load_batch_rows,
)
from bench.experiment.protocol import ExperimentProtocol
from bench.experiment.store import ExperimentStore
from bench.report import (
    ModelArmSummary,
    PairedComparison,
    model_arm_matrix,
    paired_pass_rate_comparison,
    render_paired_markdown,
)

_COMPARABLE_STATUSES = frozenset({"completed", "completed_with_errors"})


@dataclass(frozen=True)
class CrossModelReport:
    comparison_id: str
    protocol_id: str
    phase: str
    budget_tier: str
    batch_ids: tuple[str, ...]
    models: tuple[str, ...]
    matrix: tuple[ModelArmSummary, ...]
    paired: tuple[PairedComparison, ...]

    def to_dict(self) -> dict[str, Any]:
        analysis = {
            item.model or f"batch-{index}": item.authorized
            for index, item in enumerate(self.paired)
        }
        claims = {
            item.model or f"batch-{index}": item.significant_difference
            for index, item in enumerate(self.paired)
        }
        return {
            "comparison_id": self.comparison_id,
            "protocol_id": self.protocol_id,
            "phase": self.phase,
            "budget_tier": self.budget_tier,
            "batch_ids": list(self.batch_ids),
            "models": list(self.models),
            "batch_analysis_authorized": [
                item.authorized for item in self.paired
            ],
            "cross_model_inferential_claim": False,
            "analysis_authorized_by_model": analysis,
            "statistical_claims_by_model": claims,
            "model_matrix": [item.to_dict() for item in self.matrix],
            "paired_analyses": [item.to_dict() for item in self.paired],
        }

    def render_markdown(self) -> str:
        lines = [
            "# 多智能体跨模型比较",
            "",
            f"- 比较：`{self.comparison_id}`",
            f"- 协议：`{self.protocol_id}`",
            f"- 阶段 / 预算：`{self.phase}` / `{self.budget_tier}`",
            f"- 批次：{', '.join(f'`{item}`' for item in self.batch_ids)}",
            f"- 模型：{', '.join(f'`{item}`' for item in self.models)}",
            "- 跨模型直接推断：**不授权**；只在同一模型内做预注册配对分析。",
            "",
            "## 模型 × Arm",
            "",
            "| 模型 | Arm | Runs | 有效样本 | 通过率 | Error | Token | 成本信号 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for item in self.matrix:
            pass_rate = (
                f"{item.pass_rate:.1%}" if item.pass_rate is not None else "-"
            )
            lines.append(
                f"| {item.model} | {item.arm} | {item.runs} | "
                f"{item.valid_runs} | {pass_rate} | {item.errors} | "
                f"{item.total_tokens} | {item.cost_signal} |"
            )
        for comparison in self.paired:
            lines.extend(["", render_paired_markdown(comparison).rstrip()])
        return "\n".join(lines) + "\n"


def build_cross_model_report(
    batch_ids: tuple[str, ...],
    *,
    output_root: Path | str,
    protocol: ExperimentProtocol,
) -> CrossModelReport:
    if len(batch_ids) < 2:
        raise ValueError("跨模型比较至少需要两个批次")
    if len(set(batch_ids)) != len(batch_ids):
        raise ValueError("跨模型比较不能重复引用同一批次")
    if protocol.statistics is None:
        raise ValueError("跨模型比较需要带 statistics 的协议")

    batches = BatchStore(output_root)
    experiments = ExperimentStore(output_root)
    manifests: list[BatchManifest] = [batches.load(item) for item in batch_ids]
    signatures = {
        tuple(
            sorted(
                (cell.task_id, cell.arm, cell.expected_trials)
                for cell in manifest.cells
            )
        )
        for manifest in manifests
    }
    if len(signatures) != 1:
        raise ValueError("跨模型批次的 Task × Arm × Trial 矩阵不一致")
    phases = {manifest.phase for manifest in manifests}
    tiers = {manifest.budget_tier for manifest in manifests}
    if len(phases) != 1 or len(tiers) != 1:
        raise ValueError("跨模型批次的阶段和预算档必须一致")

    rows = []
    batch_models: dict[str, str] = {}
    for manifest in manifests:
        if manifest.status not in _COMPARABLE_STATUSES:
            raise ValueError(
                f"批次 {manifest.batch_id} 状态为 {manifest.status!r}，不能比较"
            )
        current = load_batch_rows(manifest, experiments)
        models = {row.model_identity for row in current if row.model_identity}
        if len(models) != 1:
            raise ValueError(
                f"批次 {manifest.batch_id} 必须只包含一个模型；观察到 {sorted(models)}"
            )
        batch_models[manifest.batch_id] = next(iter(models))
        rows.extend(current)

    model_names = tuple(sorted(batch_models.values()))
    if len(set(model_names)) != len(model_names):
        raise ValueError("每个模型只能由一个批次提供，不能合并重复模型批次")

    expected_tasks = tuple(task.task_id for task in protocol.tasks)
    expected_task_origins = {
        task.task_id: task.origin for task in protocol.tasks
    }
    paired = tuple(
        paired_pass_rate_comparison(
            rows,
            protocol.statistics,
            model=model_identity,
            expected_tasks=expected_tasks,
            expected_task_origins=expected_task_origins,
        )
        for model_identity in model_names
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    identity_display = {
        row.model_identity: row.model for row in rows if row.model_identity
    }
    display_models = tuple(
        identity_display.get(identity, identity) for identity in model_names
    )
    return CrossModelReport(
        comparison_id=f"cross-model-{stamp}-{uuid.uuid4().hex[:8]}",
        protocol_id=protocol.protocol_id,
        phase=manifests[0].phase,
        budget_tier=manifests[0].budget_tier,
        batch_ids=batch_ids,
        models=display_models,
        matrix=model_arm_matrix(rows),
        paired=paired,
    )


def write_cross_model_report(
    output_root: Path | str,
    report: CrossModelReport,
) -> tuple[Path, Path]:
    root = Path(output_root) / "_comparisons" / report.comparison_id
    root.mkdir(parents=True, exist_ok=False)
    json_path = root / "comparison.json"
    markdown_path = root / "comparison.md"
    _atomic_write(
        json_path,
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write(markdown_path, report.render_markdown())
    return json_path, markdown_path


def _atomic_write(path: Path, content: str) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(content, encoding="utf-8")
    try:
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


__all__ = [
    "CrossModelReport",
    "build_cross_model_report",
    "write_cross_model_report",
]
