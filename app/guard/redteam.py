from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

BLOCKED_ACTIONS = ("deny", "review")


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    category: str
    expect_blocked: bool
    action: str
    policy_ids: tuple[str, ...]
    correct: bool


@dataclass(frozen=True)
class CategoryStats:
    total: int = 0
    true_positive: int = 0
    false_negative: int = 0
    false_positive: int = 0
    true_negative: int = 0
    recall: float = 0.0
    precision: float = 0.0
    false_positive_rate: float = 0.0


@dataclass(frozen=True)
class RedTeamReport:
    total: int = 0
    true_positive: int = 0
    false_negative: int = 0
    false_positive: int = 0
    true_negative: int = 0
    recall: float = 0.0
    precision: float = 0.0
    false_positive_rate: float = 0.0
    by_category: dict[str, CategoryStats] = field(default_factory=dict)
    details: list[CaseResult] = field(default_factory=list)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _normalize_action(action: object) -> str:
    return action.value if hasattr(action, "value") else str(action)


def _stats(tp: int, fn: int, fp: int, tn: int) -> CategoryStats:
    return CategoryStats(
        total=tp + fn + fp + tn,
        true_positive=tp,
        false_negative=fn,
        false_positive=fp,
        true_negative=tn,
        recall=_ratio(tp, tp + fn),
        precision=_ratio(tp, tp + fp),
        false_positive_rate=_ratio(fp, fp + tn),
    )


def run(
    cases: list[dict],
    evaluator: Callable[[str], tuple[str, tuple[str, ...]]],
) -> RedTeamReport:
    counts = {"tp": 0, "fn": 0, "fp": 0, "tn": 0}
    per_category: dict[str, dict[str, int]] = {}
    details: list[CaseResult] = []
    for case in cases:
        case_id = case.get("id", "")
        category = case.get("category", "unknown")
        expect_blocked = bool(case.get("expect_blocked", False))
        action, policy_ids = evaluator(case.get("output", ""))
        action_value = _normalize_action(action)
        blocked = action_value in BLOCKED_ACTIONS
        if expect_blocked and blocked:
            bucket = "tp"
        elif expect_blocked and not blocked:
            bucket = "fn"
        elif not expect_blocked and not blocked:
            bucket = "tn"
        else:
            bucket = "fp"
        counts[bucket] += 1
        per_category.setdefault(category, {"tp": 0, "fn": 0, "fp": 0, "tn": 0})[bucket] += 1
        details.append(
            CaseResult(
                case_id=case_id,
                category=category,
                expect_blocked=expect_blocked,
                action=action_value,
                policy_ids=tuple(policy_ids),
                correct=bucket in ("tp", "tn"),
            )
        )
    by_category = {cat: _stats(**buckets) for cat, buckets in sorted(per_category.items())}
    return RedTeamReport(
        total=len(cases),
        true_positive=counts["tp"],
        false_negative=counts["fn"],
        false_positive=counts["fp"],
        true_negative=counts["tn"],
        recall=_ratio(counts["tp"], counts["tp"] + counts["fn"]),
        precision=_ratio(counts["tp"], counts["tp"] + counts["fp"]),
        false_positive_rate=_ratio(counts["fp"], counts["fp"] + counts["tn"]),
        by_category=by_category,
        details=details,
    )


__all__ = [
    "run",
    "RedTeamReport",
    "CategoryStats",
    "CaseResult",
    "BLOCKED_ACTIONS",
]
