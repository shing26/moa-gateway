"""审计 JSONL 的读取与聚合（M3 拆分：原 dashboard.py 的数据函数）。

只做"读 logs/audit-*.jsonl -> 内存聚合"，不感知 HTTP 与 HTML；
dashboard 路由与渲染层都从这里取数。文件损坏、缺字段一律跳过，
绝不因日志脏数据让页面 500。
"""

from __future__ import annotations

import datetime
import json
import pathlib
from typing import Any


def read_recent_logs(count: int = 50) -> list[dict[str, Any]]:
    log_dir = pathlib.Path("logs")
    if not log_dir.exists():
        return []
    files = sorted(log_dir.glob("audit-*.jsonl"), key=lambda p: p.name, reverse=True)[:3]
    entries: list[dict[str, Any]] = []
    for path in files:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return entries[:count]


def load_audit_entries(days: int = 7) -> list[dict[str, Any]]:
    log_dir = pathlib.Path("logs")
    if not log_dir.exists():
        return []
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days - 1)
    entries: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("audit-*.jsonl"), key=lambda p: p.name, reverse=True):
        try:
            file_date = datetime.date.fromisoformat(path.name[len("audit-"):-len(".jsonl")])
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            ts = data.get("timestamp", "")
            if not ts:
                continue
            try:
                datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            entries.append(data)
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return entries


def trend_by_day(entries: list[dict[str, Any]], days: int = 7) -> list[dict[str, Any]]:
    today = datetime.date.today()
    dates = [(today - datetime.timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    buckets = {d: {"date": d, "deny": 0, "review": 0} for d in dates}
    for entry in entries:
        ts = entry.get("timestamp", "")
        try:
            day = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date().isoformat()
        except (TypeError, ValueError):
            continue
        if day not in buckets:
            continue
        action = str(entry.get("guard_action", ""))
        if action == "deny":
            buckets[day]["deny"] += 1
        elif action == "review":
            buckets[day]["review"] += 1
    return [buckets[d] for d in dates]


def top_risky_sessions(entries: list[dict[str, Any]], top_n: int = 10) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for entry in entries:
        action = str(entry.get("guard_action", ""))
        if action not in ("deny", "review"):
            continue
        sid = str(entry.get("session_id", "") or "unknown")
        item = agg.setdefault(sid, {"session_id": sid, "count": 0, "timestamp": "", "recent_violation": ""})
        item["count"] += 1
        ts = str(entry.get("timestamp", ""))
        if ts > item["timestamp"]:
            item["timestamp"] = ts
            hits = entry.get("policy_hits")
            if isinstance(hits, list) and hits:
                item["recent_violation"] = str(hits[0])
            else:
                item["recent_violation"] = str(entry.get("violation", ""))
    rows = sorted(agg.values(), key=lambda r: (-r["count"], r["session_id"]))[:top_n]
    for row in rows:
        row.pop("timestamp", None)
    return rows


def percentile(sorted_values: list[float], p: int) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    pos = (p / 100.0) * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def hitl_latency_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    durations: list[float] = []
    for entry in entries:
        if not entry.get("hitl_decision"):
            continue
        try:
            value = float(entry.get("hitl_duration_ms", 0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            durations.append(value)
    if not durations:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0,
                "buckets": {"lt30": 0, "30to120": 0, "gt120": 0}}
    sorted_d = sorted(durations)
    return {
        "count": len(sorted_d),
        "p50_ms": round(percentile(sorted_d, 50), 1),
        "p95_ms": round(percentile(sorted_d, 95), 1),
        "max_ms": round(sorted_d[-1], 1),
        "buckets": {
            "lt30": sum(1 for d in sorted_d if d < 30_000),
            "30to120": sum(1 for d in sorted_d if 30_000 <= d < 120_000),
            "gt120": sum(1 for d in sorted_d if d >= 120_000),
        },
    }


__all__ = [
    "hitl_latency_stats",
    "load_audit_entries",
    "percentile",
    "read_recent_logs",
    "top_risky_sessions",
    "trend_by_day",
]
