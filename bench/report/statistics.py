"""Pre-registered paired analysis and cross-model summaries for experiment data."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bench.report.aggregate import RunRow

if TYPE_CHECKING:
    from bench.experiment.protocol import ProtocolStatistics

_VALID_OUTCOMES = frozenset({"pass", "fail", "budget_exhausted"})


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile 需要非空样本")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _bootstrap_interval(
    differences: list[float],
    *,
    resamples: int,
    confidence_level: float,
    seed: int,
) -> tuple[float, float]:
    rng = random.Random(seed)
    count = len(differences)
    means = [
        sum(differences[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(resamples)
    ]
    alpha = 1 - confidence_level
    return (
        _percentile(means, alpha / 2),
        _percentile(means, 1 - alpha / 2),
    )


def _sign_flip_p_value(
    differences: list[float],
    *,
    resamples: int,
    seed: int,
) -> float:
    observed = abs(_mean(differences))
    count = len(differences)
    if count == 0:
        return 1.0
    if count <= 16:
        extreme = 0
        total = 1 << count
        for mask in range(total):
            flipped = [
                -value if mask & (1 << index) else value
                for index, value in enumerate(differences)
            ]
            if abs(_mean(flipped)) >= observed - 1e-12:
                extreme += 1
        return extreme / total
    rng = random.Random(seed)
    extreme = 0
    for _ in range(resamples):
        flipped_mean = _mean(
            [
                -value if rng.random() < 0.5 else value
                for value in differences
            ]
        )
        if abs(flipped_mean) >= observed - 1e-12:
            extreme += 1
    return extreme / resamples


@dataclass(frozen=True)
class PairedTask:
    task_id: str
    task_origin: str
    candidate_valid_runs: int
    baseline_valid_runs: int
    candidate_pass_rate: float | None
    baseline_pass_rate: float | None
    difference: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_origin": self.task_origin,
            "candidate_valid_runs": self.candidate_valid_runs,
            "baseline_valid_runs": self.baseline_valid_runs,
            "candidate_pass_rate": self.candidate_pass_rate,
            "baseline_pass_rate": self.baseline_pass_rate,
            "difference": self.difference,
        }


@dataclass(frozen=True)
class PairedComparison:
    authorized: bool
    decision: str
    block_reasons: tuple[str, ...]
    model: str
    candidate_arm: str
    baseline_arm: str
    primary_metric: str
    unit: str
    planned_task_count: int
    observed_task_count: int
    tasks: tuple[PairedTask, ...]
    mean_difference: float | None
    confidence_level: float | None
    confidence_interval: tuple[float, float] | None
    p_value: float | None
    method: str
    sensitivity: dict[str, Any]

    @property
    def significant_difference(self) -> bool:
        return self.authorized and self.decision in {
            "candidate_better",
            "candidate_worse",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorized": self.authorized,
            "decision": self.decision,
            "block_reasons": list(self.block_reasons),
            "model": self.model,
            "candidate_arm": self.candidate_arm,
            "baseline_arm": self.baseline_arm,
            "primary_metric": self.primary_metric,
            "unit": self.unit,
            "planned_task_count": self.planned_task_count,
            "observed_task_count": self.observed_task_count,
            "tasks": [item.to_dict() for item in self.tasks],
            "mean_difference": self.mean_difference,
            "confidence_level": self.confidence_level,
            "confidence_interval": (
                list(self.confidence_interval)
                if self.confidence_interval is not None
                else None
            ),
            "p_value": self.p_value,
            "method": self.method,
            "sensitivity": self.sensitivity,
        }


def paired_pass_rate_comparison(
    rows: Iterable[RunRow],
    preregistration: ProtocolStatistics,
    *,
    model: str | None = None,
    expected_tasks: Iterable[str] | None = None,
    expected_task_origins: Mapping[str, str] | None = None,
) -> PairedComparison:
    all_rows = list(rows)
    models = sorted({row.model_identity for row in all_rows if row.model_identity})
    selected_model = model or (models[0] if len(models) == 1 else "")
    block_reasons: list[str] = []
    if len(models) != 1 and model is None:
        block_reasons.append(
            f"必须显式选择一个模型；观察到 {len(models)} 个模型"
        )
    if model is not None and model not in models:
        block_reasons.append(f"输入数据不包含模型 {model}")

    selected = [
        row
        for row in all_rows
        if selected_model and row.model_identity == selected_model
    ]
    wanted_arms = {
        preregistration.candidate_arm,
        preregistration.baseline_arm,
    }
    selected = [row for row in selected if row.arm in wanted_arms]

    grouped: dict[tuple[str, str], list[RunRow]] = defaultdict(list)
    for row in selected:
        grouped[(row.task_id, row.arm)].append(row)
    if expected_tasks is None:
        task_ids = sorted({row.task_id for row in selected})
    else:
        task_ids = sorted({str(task) for task in expected_tasks})

    tasks: list[PairedTask] = []
    for task_id in task_ids:
        candidate = grouped.get(
            (task_id, preregistration.candidate_arm), []
        )
        baseline = grouped.get(
            (task_id, preregistration.baseline_arm), []
        )
        candidate_valid = [
            row for row in candidate if row.outcome in _VALID_OUTCOMES
        ]
        baseline_valid = [
            row for row in baseline if row.outcome in _VALID_OUTCOMES
        ]
        if not candidate:
            block_reasons.append(
                f"{task_id} 缺少 {preregistration.candidate_arm}"
            )
        if not baseline:
            block_reasons.append(
                f"{task_id} 缺少 {preregistration.baseline_arm}"
            )
        if len(candidate_valid) < preregistration.min_valid_repeats_per_cell:
            block_reasons.append(
                f"{task_id} {preregistration.candidate_arm} 有效样本 "
                f"{len(candidate_valid)} < "
                f"{preregistration.min_valid_repeats_per_cell}"
            )
        if len(baseline_valid) < preregistration.min_valid_repeats_per_cell:
            block_reasons.append(
                f"{task_id} {preregistration.baseline_arm} 有效样本 "
                f"{len(baseline_valid)} < "
                f"{preregistration.min_valid_repeats_per_cell}"
            )
        candidate_rate = (
            sum(row.outcome == "pass" for row in candidate_valid)
            / len(candidate_valid)
            if candidate_valid
            else None
        )
        baseline_rate = (
            sum(row.outcome == "pass" for row in baseline_valid)
            / len(baseline_valid)
            if baseline_valid
            else None
        )
        difference = (
            candidate_rate - baseline_rate
            if candidate_rate is not None and baseline_rate is not None
            else None
        )
        origin = (
            candidate[0].task_origin
            if candidate
            else baseline[0].task_origin
            if baseline
            else (expected_task_origins or {}).get(task_id, "E1")
        )
        tasks.append(
            PairedTask(
                task_id=task_id,
                task_origin=origin,
                candidate_valid_runs=len(candidate_valid),
                baseline_valid_runs=len(baseline_valid),
                candidate_pass_rate=candidate_rate,
                baseline_pass_rate=baseline_rate,
                difference=difference,
            )
        )

    if len(task_ids) != preregistration.planned_task_count:
        block_reasons.append(
            f"配对任务数 {len(task_ids)} != 计划 "
            f"{preregistration.planned_task_count}"
        )
    differences = [
        item.difference for item in tasks if item.difference is not None
    ]
    if len(differences) != len(task_ids):
        block_reasons.append("存在无法形成配对差值的任务")

    authorized = not block_reasons
    mean_difference = _mean(differences) if differences else None
    interval: tuple[float, float] | None = None
    p_value: float | None = None
    decision = "blocked"
    if authorized:
        interval = _bootstrap_interval(
            differences,
            resamples=preregistration.bootstrap_resamples,
            confidence_level=preregistration.confidence_level,
            seed=preregistration.random_seed,
        )
        p_value = _sign_flip_p_value(
            differences,
            resamples=preregistration.bootstrap_resamples,
            seed=preregistration.random_seed,
        )
        if interval[0] > 0 and p_value < preregistration.alpha:
            decision = "candidate_better"
        elif interval[1] < 0 and p_value < preregistration.alpha:
            decision = "candidate_worse"
        else:
            decision = "no_significant_difference"

    sensitivity = _sensitivity(tasks, preregistration)
    display_model = next(
        (row.model for row in selected if row.model_identity == selected_model),
        selected_model,
    )
    return PairedComparison(
        authorized=authorized,
        decision=decision,
        block_reasons=tuple(dict.fromkeys(block_reasons)),
        model=display_model,
        candidate_arm=preregistration.candidate_arm,
        baseline_arm=preregistration.baseline_arm,
        primary_metric=preregistration.primary_metric,
        unit=preregistration.unit,
        planned_task_count=preregistration.planned_task_count,
        observed_task_count=len(task_ids),
        tasks=tuple(tasks),
        mean_difference=mean_difference,
        confidence_level=(
            preregistration.confidence_level if authorized else None
        ),
        confidence_interval=interval,
        p_value=p_value,
        method="paired_task_bootstrap_percentile_with_exact_sign_flip",
        sensitivity=sensitivity,
    )


def _sensitivity(
    tasks: list[PairedTask], preregistration: ProtocolStatistics
) -> dict[str, Any]:
    paired = [item for item in tasks if item.difference is not None]
    output: dict[str, Any] = {}
    if "leave_one_task_out" in preregistration.sensitivity:
        values = []
        for omitted in paired:
            remaining = [
                item.difference
                for item in paired
                if item.task_id != omitted.task_id
            ]
            values.append(_mean(remaining))
        output["leave_one_task_out"] = {
            "min_mean_difference": min(values) if values else None,
            "max_mean_difference": max(values) if values else None,
        }
    if "origin_stratified" in preregistration.sensitivity:
        origins: dict[str, list[float]] = defaultdict(list)
        for item in paired:
            origins[item.task_origin].append(item.difference)
        output["origin_stratified"] = [
            {
                "task_origin": origin,
                "tasks": len(values),
                "mean_difference": _mean(values),
                "inferential_claim": False,
            }
            for origin, values in sorted(origins.items())
        ]
    return output


@dataclass(frozen=True)
class ModelArmSummary:
    model: str
    model_identity: str
    arm: str
    runs: int
    valid_runs: int
    pass_rate: float | None
    errors: int
    total_tokens: int
    total_cost_usd: float
    cost_signal: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "model_identity": self.model_identity,
            "arm": self.arm,
            "runs": self.runs,
            "valid_runs": self.valid_runs,
            "pass_rate": self.pass_rate,
            "errors": self.errors,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "cost_signal": self.cost_signal,
        }


def model_arm_matrix(rows: Iterable[RunRow]) -> tuple[ModelArmSummary, ...]:
    grouped: dict[tuple[str, str], list[RunRow]] = defaultdict(list)
    for row in rows:
        grouped[(row.model_identity or "<unknown>", row.arm)].append(row)
    output = []
    for (identity, arm), chosen in sorted(grouped.items()):
        valid = [row for row in chosen if row.outcome in _VALID_OUTCOMES]
        display_model = next((row.model for row in chosen if row.model), "")
        passes = sum(row.outcome == "pass" for row in valid)
        costs = [row.cost_usd for row in chosen]
        if all(cost == 0 for cost in costs):
            cost_signal = "unavailable_or_zero"
        elif any(cost == 0 for cost in costs):
            cost_signal = "mixed_or_unpriced"
        else:
            cost_signal = "available"
        output.append(
            ModelArmSummary(
                model=display_model,
                model_identity=identity,
                arm=arm,
                runs=len(chosen),
                valid_runs=len(valid),
                pass_rate=passes / len(valid) if valid else None,
                errors=sum(row.outcome == "error" for row in chosen),
                total_tokens=sum(row.total_tokens for row in chosen),
                total_cost_usd=sum(costs),
                cost_signal=cost_signal,
            )
        )
    return tuple(output)


def render_paired_markdown(comparison: PairedComparison) -> str:
    lines = [
        "## 预注册配对分析",
        "",
        (
            f"- 对比：`{comparison.candidate_arm}` - "
            f"`{comparison.baseline_arm}`；单位 `{comparison.unit}`；"
            f"主指标 `{comparison.primary_metric}`"
        ),
        f"- 模型：`{comparison.model or '<unresolved>'}`",
        (
            f"- 计划任务数：{comparison.planned_task_count}；"
            f"实际配对任务数：{comparison.observed_task_count}"
        ),
    ]
    if not comparison.authorized:
        lines.append("- 分析门禁：**未授权**")
        lines.append(
            "- 阻止原因：" + "；".join(comparison.block_reasons)
        )
    else:
        assert comparison.confidence_interval is not None
        low, high = comparison.confidence_interval
        lines.append("- 分析门禁：**已授权并满足预注册门禁**")
        lines.append(
            f"- 平均配对差值：{comparison.mean_difference:+.3f}"
            if comparison.mean_difference is not None
            else "- 平均配对差值：-"
        )
        lines.append(
            f"- {comparison.confidence_level:.0%} 配对 bootstrap 区间："
            f"[{low:+.3f}, {high:+.3f}]；符号翻转 p="
            f"{comparison.p_value:.4f}"
        )
        lines.append(f"- 决策：`{comparison.decision}`")
        lines.append(
            "- 显著差异声明：**已授权**"
            if comparison.significant_difference
            else "- 显著差异声明：**不授权**；不得宣称 arm 更优或更差"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "ModelArmSummary",
    "PairedComparison",
    "PairedTask",
    "model_arm_matrix",
    "paired_pass_rate_comparison",
    "render_paired_markdown",
]
