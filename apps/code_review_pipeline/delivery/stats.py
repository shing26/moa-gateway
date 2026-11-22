"""Read-only aggregate metrics over persisted delivery run records."""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

DELIVERED_STATE = "merged"
FAILED_STATES = frozenset(
    {"failed", "budget_exhausted", "rejected", "needs_human"}
)
RETRY_PREFIX = "retry:"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _local_date(value: datetime) -> date:
    if value.tzinfo is not None:
        return value.astimezone().date()
    return value.date()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_int(value: Any) -> int:
    return int(value) if _is_number(value) else 0


def _as_float(value: Any) -> float:
    return float(value) if _is_number(value) else 0.0


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def _failure_code(payload: dict[str, Any]) -> str | None:
    failure = payload.get("failure")
    if not isinstance(failure, dict):
        return None
    code = failure.get("code")
    if isinstance(code, str) and code.strip():
        return code.strip()
    return None


def _source_ref(payload: dict[str, Any]) -> str:
    request = payload.get("request")
    if not isinstance(request, dict):
        return ""
    source_ref = request.get("source_ref")
    return source_ref if isinstance(source_ref, str) else ""


def collect_stats(
    directory: str | Path,
    *,
    since: date | None = None,
) -> dict[str, Any]:
    """Aggregate ``*.json`` delivery records without mutating them.

    Unreadable records (invalid JSON, missing state, or a missing/unparseable
    ``started_at``) are counted in ``unreadable`` and never silently dropped.
    """
    root = Path(directory)
    paths = sorted(root.glob("*.json")) if root.is_dir() else []

    runs = 0
    unreadable = 0
    delivered = 0
    failed = 0
    in_flight = 0
    by_state: Counter[str] = Counter()
    failure_codes: Counter[str | None] = Counter()
    durations: list[float] = []
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    cost_usd = 0.0
    retries = 0
    retried_merged = 0

    for path in paths:
        payload = _load(path)
        if payload is None:
            runs += 1
            unreadable += 1
            continue

        state = payload.get("state")
        started_at = _parse_datetime(payload.get("started_at"))
        if (
            started_at is None
            or not isinstance(state, str)
            or not state.strip()
        ):
            runs += 1
            unreadable += 1
            continue
        state = state.strip()
        if since is not None and _local_date(started_at) < since:
            continue

        runs += 1
        by_state[state] += 1
        is_delivered = state == DELIVERED_STATE
        is_failed = state in FAILED_STATES
        if is_delivered:
            delivered += 1
        elif is_failed:
            failed += 1
        else:
            in_flight += 1

        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        raw_total = usage.get("total_tokens")
        total = _as_int(raw_total) if _is_number(raw_total) else prompt + completion
        prompt_tokens += prompt
        completion_tokens += completion
        total_tokens += total
        cost_usd += _as_float(usage.get("cost_usd"))

        failure_codes[_failure_code(payload)] += 1

        source_ref = _source_ref(payload)
        if source_ref.startswith(RETRY_PREFIX):
            retries += 1
            if is_delivered:
                retried_merged += 1

        if is_delivered or is_failed:
            updated_at = _parse_datetime(payload.get("updated_at"))
            if updated_at is not None:
                try:
                    durations.append((updated_at - started_at).total_seconds())
                except (TypeError, OverflowError):
                    pass

    terminal = delivered + failed

    def average(total: float) -> float | None:
        return total / terminal if terminal else None

    ordered_failures = sorted(
        failure_codes.items(),
        key=lambda item: (
            -item[1],
            item[0] if item[0] is not None else "\uffff",
        ),
    )

    return {
        "runs": runs,
        "unreadable": unreadable,
        "by_state": {state: by_state[state] for state in sorted(by_state)},
        "delivered": delivered,
        "failed": failed,
        "in_flight": in_flight,
        "completion_rate": average(delivered),
        "tokens": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "avg_prompt_tokens": average(prompt_tokens),
            "avg_completion_tokens": average(completion_tokens),
            "avg_total_tokens": average(total_tokens),
            "avg_cost_usd": average(cost_usd),
        },
        "duration": {
            "p50": _percentile(durations, 0.5),
            "p90": _percentile(durations, 0.9),
        },
        "failure_codes": [
            {"code": code, "count": count}
            for code, count in ordered_failures
        ],
        "retries": {
            "total": retries,
            "merged": retried_merged,
            "merged_rate": (
                retried_merged / retries if retries else None
            ),
        },
    }


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.2f}%"


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}s"


def _average(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def format_stats(
    stats: dict[str, Any],
    *,
    directory: str | Path | None = None,
    since: date | None = None,
) -> str:
    """Render aggregate stats as an aligned dependency-free text table."""
    lines: list[str] = []
    if directory is not None:
        lines.append(f"delivery runs: {directory}")
    if since is not None:
        lines.append(f"since        : {since.isoformat()}")
    lines.extend(
        [
            f"runs        : {stats['runs']}",
            f"unreadable  : {stats['unreadable']}",
            f"delivered   : {stats['delivered']}",
            f"failed      : {stats['failed']}",
            f"in_flight   : {stats['in_flight']}",
            f"completion  : {_rate(stats['completion_rate'])}",
            "",
            "by state:",
        ]
    )
    if stats["by_state"]:
        for state, count in stats["by_state"].items():
            lines.append(f"  {state:<20} {count:>6}")
    else:
        lines.append("  -")

    tokens = stats["tokens"]
    lines.extend(
        [
            "",
            "tokens (sum / avg terminal):",
            (
                f"  {'prompt':<12} {tokens['prompt_tokens']:>10} / "
                f"{_average(tokens['avg_prompt_tokens']):>10}"
            ),
            (
                f"  {'completion':<12} {tokens['completion_tokens']:>10} / "
                f"{_average(tokens['avg_completion_tokens']):>10}"
            ),
            (
                f"  {'total':<12} {tokens['total_tokens']:>10} / "
                f"{_average(tokens['avg_total_tokens']):>10}"
            ),
            (
                f"  {'cost_usd':<12} {tokens['cost_usd']:>10.6f} / "
                f"{_average(tokens['avg_cost_usd']):>10}"
            ),
            "",
            f"duration p50: {_seconds(stats['duration']['p50'])}",
            f"duration p90: {_seconds(stats['duration']['p90'])}",
            "",
            "failure codes:",
        ]
    )
    if stats["failure_codes"]:
        for item in stats["failure_codes"]:
            code = item["code"] if item["code"] is not None else "(none)"
            lines.append(f"  {code:<24} {item['count']:>6}")
    else:
        lines.append("  -")

    retries = stats["retries"]
    lines.extend(
        [
            "",
            (
                f"retries     : {retries['total']} "
                f"(merged {retries['merged']}, "
                f"rate {_rate(retries['merged_rate'])})"
            ),
        ]
    )
    return "\n".join(lines)
