"""Deterministic daily governance digest.

Reads the merge channel and delivery-run records, renders a short notification
card body, and owns the once-per-local-day send state.  No write path to GitHub
or the delivery state machine is involved.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from apps.code_review_pipeline.delivery.stats import collect_stats

logger = logging.getLogger("moa.delivery.digest")

PENDING_DELIVERY_STATES = frozenset(
    {"ready_for_push_approval", "awaiting_merge_approval"}
)
STUCK_DELIVERY_STATES = frozenset({"needs_human", "budget_exhausted", "failed"})
IN_PROGRESS_DELIVERY_STATES = frozenset(
    {
        "preparing",
        "implementing",
        "checking",
        "verifier_review",
        "revising",
        "pushing",
        "waiting_ci",
        "merging",
    }
)
IN_PROGRESS_MERGE_STATES = frozenset({"watching", "ci_failed", "merging"})
_MAX_SECTION_ITEMS = 3
_MAX_BODY_LINES = 10


@dataclass(frozen=True)
class DigestItem:
    text: str
    url: str = ""
    age_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "url": self.url}


@dataclass(frozen=True)
class DigestState:
    last_sent_date: str = ""
    last_attempt_at: str = ""
    message_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "last_sent_date": self.last_sent_date,
            "last_attempt_at": self.last_attempt_at,
            "message_id": self.message_id,
        }


@dataclass(frozen=True)
class DigestReport:
    generated_at: str
    text: str
    pending: dict[str, Any] = field(default_factory=dict)
    stuck: dict[str, Any] = field(default_factory=dict)
    in_progress: dict[str, Any] = field(default_factory=dict)
    last_24h: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "text": self.text,
            "pending": dict(self.pending),
            "stuck": dict(self.stuck),
            "in_progress": dict(self.in_progress),
            "last_24h": dict(self.last_24h),
        }


def state_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / "state.json"


def load_state(path: str | Path) -> DigestState:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return DigestState()
    if not isinstance(payload, dict):
        return DigestState()
    return DigestState(
        last_sent_date=str(payload.get("last_sent_date") or ""),
        last_attempt_at=str(payload.get("last_attempt_at") or ""),
        message_id=str(payload.get("message_id") or ""),
    )


def save_state(path: str | Path, state: DigestState) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(state.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _elapsed_seconds(now: datetime, then: datetime | None) -> float | None:
    if then is None:
        return None
    if then.tzinfo is None and now.tzinfo is not None:
        then = then.replace(tzinfo=now.tzinfo)
    elif then.tzinfo is not None and now.tzinfo is None:
        then = then.astimezone().replace(tzinfo=None)
    try:
        return (now - then).total_seconds()
    except (TypeError, OverflowError):
        return None


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "时长未知"
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, remainder = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if remainder == 0 else f"{hours}h{remainder:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d" if hours == 0 else f"{days}d{hours}h"


def _summary(exc: BaseException) -> str:
    message = str(exc).strip() or type(exc).__name__
    return " ".join(message.split())[:160]


def summarize_exception(exc: BaseException) -> str:
    return _summary(exc)


def should_attempt(
    now: datetime,
    *,
    hour: int,
    state: DigestState,
    retry_after: timedelta = timedelta(minutes=15),
) -> bool:
    """Whether the digest may be attempted now (local-day idempotency)."""
    if now.hour < hour:
        return False
    if state.last_sent_date == now.date().isoformat():
        return False
    last_attempt = _elapsed_seconds(
        now, _parse_datetime(state.last_attempt_at)
    )
    if last_attempt is not None and last_attempt < retry_after.total_seconds():
        return False
    return True


def _merge_url(repo: str, pr_number: int) -> str:
    if "/" not in repo or pr_number <= 0:
        return ""
    return f"https://github.com/{repo}/pull/{pr_number}"


def _merge_item(record: Any, runs_by_id: dict[str, dict[str, Any]], now: datetime) -> DigestItem:
    run = runs_by_id.get(getattr(record, "run_id", "") or "")
    title = ""
    if run is not None:
        request = run.get("request")
        if isinstance(request, dict):
            title = str(request.get("title") or "")
    label = f"PR #{record.pr_number}"
    if title:
        label = f"{label} {title}"
    ci_state = str(getattr(record, "ci_state", "") or "")
    ci_text = {
        "success": "CI 绿",
        "failure": "CI 红",
    }.get(ci_state, f"CI {ci_state}" if ci_state else "")
    transitions = getattr(record, "transitions", ()) or ()
    at = transitions[-1].get("at") if transitions and isinstance(transitions[-1], dict) else None
    seconds = _elapsed_seconds(now, _parse_datetime(at))
    parts = [label]
    if ci_text:
        parts.append(ci_text)
    parts.append(_format_age(seconds))
    url = _merge_url(record.repo, record.pr_number)
    parts.append(url or "（无链接）")
    return DigestItem(text=" · ".join(parts), url=url, age_seconds=seconds)


def _run_identity(payload: dict[str, Any]) -> tuple[str, int, str]:
    request = payload.get("request")
    repo = str(request.get("repo") or "") if isinstance(request, dict) else ""
    push = payload.get("push")
    pull_request = push.get("pull_request") if isinstance(push, dict) else None
    if not isinstance(pull_request, dict):
        return ("", 0, "")
    try:
        number = int(pull_request.get("number") or 0)
    except (TypeError, ValueError):
        number = 0
    return (repo, number, str(pull_request.get("head_sha") or ""))


def _run_url(payload: dict[str, Any]) -> str:
    push = payload.get("push")
    pull_request = push.get("pull_request") if isinstance(push, dict) else None
    if not isinstance(pull_request, dict):
        return ""
    url = pull_request.get("url")
    return url if isinstance(url, str) else ""


def _run_item(
    payload: dict[str, Any],
    *,
    label: str,
    now: datetime,
) -> DigestItem:
    seconds = _elapsed_seconds(now, _parse_datetime(payload.get("updated_at")))
    url = _run_url(payload)
    text = f"{label} · {_format_age(seconds)} · {url or '（无链接）'}"
    return DigestItem(text=text, url=url, age_seconds=seconds)


def _read_runs(run_dir: str | Path) -> tuple[list[dict[str, Any]], str, bool]:
    root = Path(run_dir)
    if not root.is_dir():
        return [], f"delivery run 目录不存在: {root}", False
    payloads: list[dict[str, Any]] = []
    try:
        paths = sorted(root.glob("*.json"))
    except OSError as exc:
        return [], _summary(exc), False
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        state = payload.get("state")
        if not isinstance(state, str) or not state.strip():
            continue
        payloads.append(payload)
    return payloads, "", True


def _truncate_section(
    items: list[DigestItem],
    remaining_lines: int,
) -> tuple[list[DigestItem], int, int]:
    """Return shown items, remaining lines, and hidden count."""
    if not items:
        return [], remaining_lines, 0
    shown = min(len(items), _MAX_SECTION_ITEMS, remaining_lines)
    truncated = shown < len(items)
    if truncated and shown == remaining_lines:
        shown -= 1
    shown = max(0, shown)
    hidden = len(items) - shown
    lines = shown + (1 if hidden else 0)
    return items[:shown], remaining_lines - lines, hidden


def build_digest_report(
    now: datetime,
    *,
    merge_records: list[Any] | None,
    merge_error: str = "",
    run_dir: str | Path,
) -> DigestReport:
    """Build the digest body from read-only snapshots."""
    payloads, run_error, run_available = _read_runs(run_dir)

    runs_by_id: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        run_id = str(payload.get("run_id") or "")
        if run_id:
            runs_by_id[run_id] = payload

    pending: list[DigestItem] = []
    merge_ids: set[str] = set()
    merge_identities: set[tuple[str, int, str]] = set()
    merge_identities_without_run: set[tuple[str, int, str]] = set()
    if merge_records is not None:
        for record in merge_records:
            if getattr(record, "state", "") != "awaiting_approval":
                continue
            pending.append(_merge_item(record, runs_by_id, now))
            run_id = getattr(record, "run_id", "") or ""
            if run_id:
                merge_ids.add(run_id)
            identity = (
                getattr(record, "repo", "") or "",
                int(getattr(record, "pr_number", 0) or 0),
                getattr(record, "head_sha", "") or "",
            )
            if identity[0] and identity[1] > 0:
                merge_identities.add(identity)
                if not run_id:
                    merge_identities_without_run.add(identity)

    if run_available:
        for payload in payloads:
            state = str(payload.get("state") or "")
            if state not in PENDING_DELIVERY_STATES:
                continue
            run_id = str(payload.get("run_id") or "")
            identity = _run_identity(payload)
            covered = bool(run_id) and run_id in merge_ids
            if identity[0] and identity[1] > 0:
                if identity in merge_identities_without_run:
                    covered = True
                elif not run_id and identity in merge_identities:
                    covered = True
            if covered:
                continue
            if state == "ready_for_push_approval":
                label = f"推送审批 {run_id or 'unknown'}"
            else:
                label = f"合并审批 {run_id or 'unknown'}"
            pending.append(_run_item(payload, label=label, now=now))

    pending.sort(
        key=lambda item: (
            item.age_seconds is None,
            -(item.age_seconds or 0.0),
        )
    )
    pending_count = len(pending)

    stuck: list[DigestItem] = []
    in_progress_delivery = 0
    if run_available:
        seven_days = 7 * 24 * 3600
        for payload in payloads:
            state = str(payload.get("state") or "")
            if state in IN_PROGRESS_DELIVERY_STATES:
                in_progress_delivery += 1
            if state not in STUCK_DELIVERY_STATES:
                continue
            seconds = _elapsed_seconds(now, _parse_datetime(payload.get("updated_at")))
            if seconds is None or seconds > seven_days:
                continue
            failure = payload.get("failure")
            code = "(none)"
            if isinstance(failure, dict):
                raw_code = failure.get("code")
                if isinstance(raw_code, str) and raw_code.strip():
                    code = raw_code.strip()
            run_id = str(payload.get("run_id") or "unknown")
            text = (
                f"{run_id} · {code} · {_format_age(seconds)} · （无链接）"
            )
            stuck.append(
                DigestItem(text=text, url="", age_seconds=seconds)
            )
    stuck.sort(
        key=lambda item: (
            item.age_seconds is None,
            -(item.age_seconds or 0.0),
        )
    )
    stuck_count = len(stuck)

    in_progress_merge = 0
    if merge_records is not None:
        in_progress_merge = sum(
            1
            for record in merge_records
            if getattr(record, "state", "") in IN_PROGRESS_MERGE_STATES
        )

    in_progress_available = run_available and merge_records is not None
    in_progress_count = in_progress_delivery + in_progress_merge

    last_24h: dict[str, Any]
    if not run_available:
        last_24h = {"unavailable": run_error}
    elif not payloads:
        last_24h = {"runs": 0, "delivered": 0, "failed": 0}
    else:
        since = now.date() - timedelta(days=1)
        stats = collect_stats(run_dir, since=since)
        last_24h = {
            "runs": int(stats["runs"]),
            "delivered": int(stats["delivered"]),
            "failed": int(stats["failed"]),
        }

    lines: list[str] = [
        f"MoA 治理摘要 · {now.strftime('%Y-%m-%d %H:%M')}",
    ]
    if merge_error and pending_count == 0:
        lines.append(f"待你决定: 不可用（{merge_error}）")
    elif merge_error:
        lines.append(
            f"待你决定: {pending_count}（merge 待批不可用: {merge_error}）"
        )
    elif pending:
        lines.append(
            f"待你决定: {pending_count}"
            f"（最早 {_format_age(pending[0].age_seconds)}）"
        )
    else:
        lines.append("待你决定: 0")

    lines.append(f"卡住了: {stuck_count if run_available else '不可用（' + run_error + '）'}")
    if in_progress_available:
        lines.append(
            f"进行中: {in_progress_count}"
            f"（delivery {in_progress_delivery} / merge {in_progress_merge}）"
        )
    else:
        reason = run_error if not run_available else f"merge store: {merge_error}"
        lines.append(f"进行中: 不可用（{reason}）")
    if not run_available:
        lines.append(f"近 24h: 不可用（{run_error}）")
    else:
        lines.append(
            "近 24h: 新开 {runs} · 合并 {delivered} · 失败 {failed}".format(
                **last_24h
            )
        )

    remaining = _MAX_BODY_LINES - len(lines)
    pending_shown, remaining, pending_hidden = _truncate_section(pending, remaining)
    pending_lines = [f"  · {item.text}" for item in pending_shown]
    if pending_hidden:
        pending_lines.append(f"  … 另有 {pending_hidden} 条")
    # Insert entries directly under their headers.
    lines[2:2] = pending_lines

    stuck_shown, _remaining, stuck_hidden = _truncate_section(stuck, remaining)
    stuck_lines = [f"  · {item.text}" for item in stuck_shown]
    if stuck_hidden:
        stuck_lines.append(f"  … 另有 {stuck_hidden} 条")
    stuck_header = 3 + len(pending_lines)
    lines[stuck_header:stuck_header] = stuck_lines

    text = "\n".join(lines[:_MAX_BODY_LINES])
    return DigestReport(
        generated_at=now.isoformat(),
        text=text,
        pending={
            "count": pending_count,
            "items": [item.to_dict() for item in pending_shown],
            "hidden": pending_hidden,
            "unavailable": merge_error,
        },
        stuck={
            "count": stuck_count,
            "items": [item.to_dict() for item in stuck_shown],
            "hidden": stuck_hidden,
            "unavailable": "" if run_available else run_error,
        },
        in_progress={
            "count": in_progress_count if in_progress_available else None,
            "delivery": in_progress_delivery if in_progress_available else None,
            "merge": in_progress_merge if in_progress_available else None,
            "unavailable": (
                ""
                if in_progress_available
                else run_error if not run_available else f"merge store: {merge_error}"
            ),
        },
        last_24h=last_24h,
    )
