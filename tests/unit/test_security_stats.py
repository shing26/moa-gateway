import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.routes.dashboard import (
    _hitl_latency_stats,
    _load_audit_entries,
    _top_risky_sessions,
    _trend_by_day,
)


def _entry(**kw):
    base = {
        "trace_id": "t1",
        "session_id": "s1",
        "timestamp": "2026-08-06T10:00:00+00:00",
        "guard_action": "allow",
        "policy_hits": [],
        "violation": "",
    }
    base.update(kw)
    return base


def test_trend_by_day_counts():
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    entries = [
        _entry(session_id="a", timestamp=f"{today}T01:00:00+00:00", guard_action="deny"),
        _entry(session_id="b", timestamp=f"{today}T02:00:00+00:00", guard_action="deny"),
        _entry(session_id="c", timestamp=f"{today}T03:00:00+00:00", guard_action="review"),
        _entry(session_id="d", timestamp=f"{yesterday}T03:00:00+00:00", guard_action="deny"),
        _entry(session_id="e", timestamp=f"{today}T04:00:00+00:00", guard_action="allow"),
    ]
    rows = _trend_by_day(entries, 7)
    assert len(rows) == 7
    by_date = {r["date"]: r for r in rows}
    assert by_date[today]["deny"] == 2
    assert by_date[today]["review"] == 1
    assert by_date[yesterday]["deny"] == 1
    assert by_date[yesterday]["review"] == 0
    for r in rows:
        if r["date"] not in (today, yesterday):
            assert r["deny"] == 0 and r["review"] == 0


def test_trend_by_day_skips_bad_timestamps_and_missing_fields():
    today = date.today().isoformat()
    entries = [
        _entry(timestamp="not-a-timestamp", guard_action="deny"),
        _entry(timestamp="", guard_action="deny"),
        {"trace_id": "no-ts", "session_id": "x"},
        _entry(timestamp=f"{today}T05:00:00+00:00", guard_action="deny"),
    ]
    rows = _trend_by_day(entries, 7)
    assert len(rows) == 7
    by_date = {r["date"]: r for r in rows}
    assert by_date[today]["deny"] == 1


def test_trend_by_day_empty_returns_zero_rows():
    rows = _trend_by_day([], 7)
    assert len(rows) == 7
    assert all(r["deny"] == 0 and r["review"] == 0 for r in rows)


def test_top_risky_sessions_aggregates_and_recent_violation():
    entries = [
        _entry(session_id="s1", timestamp="2026-08-06T01:00:00+00:00", guard_action="deny",
               policy_hits=["ip.internal"]),
        _entry(session_id="s1", timestamp="2026-08-06T02:00:00+00:00", guard_action="review",
               policy_hits=["secret.leak"]),
        _entry(session_id="s2", timestamp="2026-08-06T03:00:00+00:00", guard_action="deny", policy_hits=[]),
        _entry(session_id="s3", timestamp="2026-08-06T04:00:00+00:00", guard_action="allow",
               policy_hits=["secret.leak"]),
        _entry(session_id="", timestamp="2026-08-06T05:00:00+00:00", guard_action="deny",
               policy_hits=["ip.internal"]),
    ]
    rows = _top_risky_sessions(entries, 10)
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["count"] == 2
    assert rows[0]["recent_violation"] == "secret.leak"
    assert {r["session_id"] for r in rows} == {"s1", "s2", "unknown"}
    assert rows[0]["count"] + rows[1]["count"] + rows[2]["count"] == 4


def test_top_risky_sessions_old_data_falls_back_to_violation():
    entries = [
        {"trace_id": "old1", "session_id": "s9", "timestamp": "2026-08-05T01:00:00+00:00",
         "guard_action": "deny", "violation": "secret.leak"},
        {"trace_id": "old2", "session_id": "s9", "timestamp": "2026-08-05T02:00:00+00:00",
         "guard_action": "deny", "violation": "no-price-commitment"},
    ]
    rows = _top_risky_sessions(entries, 10)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s9"
    assert rows[0]["count"] == 2
    assert rows[0]["recent_violation"] == "no-price-commitment"


def test_top_risky_sessions_non_list_policy_hits_tolerated():
    entries = [
        {"trace_id": "o1", "session_id": "s7", "timestamp": "2026-08-05T01:00:00+00:00",
         "guard_action": "deny", "policy_hits": "secret.leak", "violation": "fallback"},
        {"trace_id": "o2", "session_id": "s7", "timestamp": "2026-08-05T02:00:00+00:00",
         "guard_action": "deny", "violation": "fallback"},
    ]
    rows = _top_risky_sessions(entries, 10)
    assert rows[0]["count"] == 2
    assert rows[0]["recent_violation"] == "fallback"


def test_top_risky_sessions_top_n_limit():
    entries = [
        _entry(session_id=f"sid-{i}", timestamp="2026-08-06T01:00:00+00:00", guard_action="deny")
        for i in range(12)
    ]
    rows = _top_risky_sessions(entries, 10)
    assert len(rows) == 10


def test_top_risky_sessions_empty():
    assert _top_risky_sessions([], 10) == []


def test_hitl_latency_stats_percentiles_even_samples():
    entries = [_entry(hitl_decision="approve", hitl_duration_ms=d) for d in (1000, 2000, 3000, 4000)]
    stats = _hitl_latency_stats(entries)
    assert stats["count"] == 4
    assert stats["p50_ms"] == 2500.0
    assert stats["p95_ms"] == 3850.0
    assert stats["max_ms"] == 4000.0


def test_hitl_latency_stats_single_sample():
    entries = [_entry(hitl_decision="reject", hitl_duration_ms=7000)]
    stats = _hitl_latency_stats(entries)
    assert stats["count"] == 1
    assert stats["p50_ms"] == 7000.0
    assert stats["p95_ms"] == 7000.0
    assert stats["max_ms"] == 7000.0


def test_hitl_latency_stats_buckets_and_filters():
    entries = [
        _entry(hitl_decision="approve", hitl_duration_ms=5000),
        _entry(hitl_decision="reject", hitl_duration_ms=30000),
        _entry(hitl_decision="approve", hitl_duration_ms=150000),
        _entry(hitl_decision="approve", hitl_duration_ms=0),
        _entry(hitl_decision="", hitl_duration_ms=90000),
        _entry(hitl_decision="approve", hitl_duration_ms="bad-value"),
    ]
    stats = _hitl_latency_stats(entries)
    assert stats["count"] == 3
    assert stats["buckets"] == {"lt30": 1, "30to120": 1, "gt120": 1}


def test_hitl_latency_stats_empty():
    stats = _hitl_latency_stats([])
    assert stats["count"] == 0
    assert stats["p50_ms"] == 0.0
    assert stats["p95_ms"] == 0.0
    assert stats["max_ms"] == 0.0
    assert stats["buckets"] == {"lt30": 0, "30to120": 0, "gt120": 0}


def test_load_audit_entries_filters_bad_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    today = date.today().isoformat()
    lines = [
        json.dumps({"trace_id": "1", "timestamp": f"{today}T01:00:00+00:00", "guard_action": "deny"}),
        "not-json",
        "",
        json.dumps({"trace_id": "2", "timestamp": "bad-timestamp", "guard_action": "deny"}),
        json.dumps({"trace_id": "3", "timestamp": f"{today}T02:00:00+00:00", "guard_action": "review"}),
    ]
    (log_dir / f"audit-{today}.jsonl").write_text("\n".join(lines), encoding="utf-8")
    entries = _load_audit_entries(7)
    assert {e["trace_id"] for e in entries} == {"1", "3"}


def test_load_audit_entries_skips_old_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = (date.today() - timedelta(days=10)).isoformat()
    today = date.today().isoformat()
    (log_dir / f"audit-{old}.jsonl").write_text(
        json.dumps({"trace_id": "old", "timestamp": f"{old}T01:00:00+00:00"}), encoding="utf-8")
    (log_dir / f"audit-{today}.jsonl").write_text(
        json.dumps({"trace_id": "new", "timestamp": f"{today}T01:00:00+00:00"}), encoding="utf-8")
    entries = _load_audit_entries(7)
    assert [e["trace_id"] for e in entries] == ["new"]


def test_load_audit_entries_missing_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _load_audit_entries(7) == []


def test_dashboard_security_page_renders():
    with TestClient(app) as client:
        res = client.get("/dashboard/security")
        assert res.status_code == 200
        assert "安全合规" in res.text
        assert "拦截趋势" in res.text
        assert "高风险会话 Top10" in res.text
        assert "人工审批耗时分布" in res.text
        assert "策略清单" in res.text


def test_dashboard_security_in_nav_of_other_pages():
    with TestClient(app) as client:
        res = client.get("/dashboard/overview")
        assert res.status_code == 200
        assert '/dashboard/security' in res.text
