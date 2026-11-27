from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.cli import build_parser, cmd_delivery_stats
from apps.code_review_pipeline.delivery.stats import collect_stats, format_stats


def _record(
    state: str,
    *,
    started_at: str | None = "2026-11-20T10:00:00",
    updated_at: str | None = "2026-11-20T10:00:10",
    prompt: int = 0,
    completion: int = 0,
    total: int | None = None,
    cost: float = 0.0,
    failure: dict[str, str] | None = None,
    source_ref: str = "",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": f"run-{state}",
        "trace_id": f"trace-{state}",
        "request": {"source_ref": source_ref},
        "state": state,
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": (
                prompt + completion if total is None else total
            ),
            "cost_usd": cost,
        },
        "failure": failure,
        "started_at": started_at,
        "updated_at": updated_at,
    }


def _write(directory: Path, name: str, payload: dict[str, object]) -> None:
    (directory / name).write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_t1_aggregates_states_tokens_and_terminal_durations(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "merged.json",
        _record(
            "merged",
            updated_at="2026-11-20T10:00:10",
            prompt=100,
            completion=50,
            cost=0.10,
        ),
    )
    _write(
        tmp_path,
        "failed.json",
        _record(
            "failed",
            updated_at="2026-11-20T10:00:20",
            prompt=200,
            completion=100,
            cost=0.20,
            failure={"code": "verify_failed", "message": "tests failed"},
        ),
    )
    _write(
        tmp_path,
        "budget.json",
        _record(
            "budget_exhausted",
            updated_at="2026-11-20T10:00:40",
            prompt=300,
            completion=150,
            cost=0.30,
            failure={"code": "infra_error", "message": "provider down"},
        ),
    )
    _write(
        tmp_path,
        "rejected.json",
        _record(
            "rejected",
            updated_at="2026-11-20T10:01:00",
            prompt=400,
            completion=200,
            cost=0.40,
            failure={"code": "approval_rejected", "message": "no"},
        ),
    )
    _write(
        tmp_path,
        "waiting.json",
        _record(
            "waiting_ci",
            updated_at="2026-11-20T10:00:05",
            prompt=500,
            completion=250,
            cost=0.50,
        ),
    )

    stats = collect_stats(tmp_path)

    assert stats["runs"] == 5
    assert stats["unreadable"] == 0
    assert stats["by_state"] == {
        "budget_exhausted": 1,
        "failed": 1,
        "merged": 1,
        "rejected": 1,
        "waiting_ci": 1,
    }
    assert stats["delivered"] == 1
    assert stats["failed"] == 3
    assert stats["in_flight"] == 1
    assert stats["completion_rate"] == 0.25

    tokens = stats["tokens"]
    assert tokens["prompt_tokens"] == 1500
    assert tokens["completion_tokens"] == 750
    assert tokens["total_tokens"] == 2250
    assert tokens["cost_usd"] == 1.5
    assert tokens["avg_prompt_tokens"] == 375.0
    assert tokens["avg_completion_tokens"] == 187.5
    assert tokens["avg_total_tokens"] == 562.5
    assert tokens["avg_cost_usd"] == 0.375

    assert stats["duration"] == {"p50": 20.0, "p90": 60.0}
    failures = {
        item["code"]: item["count"] for item in stats["failure_codes"]
    }
    assert failures == {
        "verify_failed": 1,
        "infra_error": 1,
        "approval_rejected": 1,
        None: 2,
    }


def test_t2_bad_json_is_counted_and_cli_exits_zero(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from app.config import settings

    _write(tmp_path, "good.json", _record("merged"))
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(settings, "delivery_run_record_dir", str(tmp_path))

    stats = collect_stats(tmp_path)
    assert stats["runs"] == 2
    assert stats["unreadable"] == 1
    assert stats["delivered"] == 1

    args = build_parser().parse_args(["delivery", "stats"])
    assert cmd_delivery_stats(args) == 0
    output = capsys.readouterr().out
    assert "unreadable" in output
    assert "1" in output


def test_t2_missing_started_at_is_unreadable(tmp_path: Path) -> None:
    _write(tmp_path, "undated.json", _record("merged", started_at=None))

    stats = collect_stats(tmp_path)

    assert stats["runs"] == 1
    assert stats["unreadable"] == 1
    assert stats["delivered"] == 0
    assert stats["by_state"] == {}


def test_t3_empty_directory_has_null_rates_and_human_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from app.config import settings

    stats = collect_stats(tmp_path)

    assert stats["runs"] == 0
    assert stats["unreadable"] == 0
    assert stats["by_state"] == {}
    assert stats["completion_rate"] is None
    assert stats["duration"] == {"p50": None, "p90": None}
    assert stats["tokens"]["total_tokens"] == 0
    assert stats["tokens"]["avg_total_tokens"] is None
    assert stats["failure_codes"] == []
    assert stats["retries"] == {
        "total": 0,
        "merged": 0,
        "merged_rate": None,
    }
    assert "runs        : 0" in format_stats(stats)
    assert "completion  : -" in format_stats(stats)

    monkeypatch.setattr(settings, "delivery_run_record_dir", str(tmp_path))
    args = build_parser().parse_args(["delivery", "stats"])
    assert cmd_delivery_stats(args) == 0
    assert "runs        : 0" in capsys.readouterr().out


def test_t4_since_filters_older_days_and_includes_threshold_day(
    tmp_path: Path,
) -> None:
    for day, state in (
        ("2026-10-31", "merged"),
        ("2026-11-01", "failed"),
        ("2026-11-02", "merged"),
    ):
        _write(
            tmp_path,
            f"{day}.json",
            _record(
                state,
                started_at=f"{day}T23:30:00",
                updated_at=f"{day}T23:30:10",
                failure=(
                    {"code": "verify_failed", "message": "x"}
                    if state == "failed"
                    else None
                ),
            ),
        )

    stats = collect_stats(tmp_path, since=date(2026, 11, 1))

    assert stats["runs"] == 2
    assert stats["delivered"] == 1
    assert stats["failed"] == 1
    assert stats["by_state"] == {"failed": 1, "merged": 1}


def test_t5_json_shape_is_stable_and_null_safe(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from app.config import settings

    stats = collect_stats(tmp_path)

    assert set(stats) == {
        "runs",
        "unreadable",
        "by_state",
        "delivered",
        "failed",
        "abandoned",
        "in_flight",
        "completion_rate",
        "success_rate",
        "tokens",
        "duration",
        "failure_codes",
        "retries",
    }
    encoded = json.dumps(stats)
    decoded = json.loads(encoded)
    assert decoded["completion_rate"] is None
    assert decoded["duration"] == {"p50": None, "p90": None}
    assert decoded["tokens"]["avg_total_tokens"] is None

    monkeypatch.setattr(settings, "delivery_run_record_dir", str(tmp_path))
    args = build_parser().parse_args(
        ["delivery", "stats", "--json", "--since", "2026-11-01"]
    )
    assert args.func is cmd_delivery_stats
    assert args.json is True
    assert args.since == "2026-11-01"
    assert cmd_delivery_stats(args) == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload["runs"] == 0
    assert cli_payload["completion_rate"] is None


def test_t6_retries_count_source_ref_prefix_and_merged_rate(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "retry-merged.json",
        _record("merged", source_ref="retry:run-1"),
    )
    _write(
        tmp_path,
        "retry-failed.json",
        _record(
            "failed",
            source_ref="retry:run-2",
            failure={"code": "verify_failed", "message": "x"},
        ),
    )
    _write(
        tmp_path,
        "issue-merged.json",
        _record("merged", source_ref="issue:42"),
    )

    stats = collect_stats(tmp_path)

    assert stats["retries"] == {
        "total": 2,
        "merged": 1,
        "merged_rate": 0.5,
    }


def test_t7_collect_and_format_do_not_write_files(tmp_path: Path) -> None:
    _write(tmp_path, "one.json", _record("merged"))
    _write(tmp_path, "two.json", _record("waiting_ci"))
    (tmp_path / "bad.json").write_text("{broken", encoding="utf-8")

    before = {
        path.name: (path.stat().st_mtime_ns, path.read_bytes())
        for path in tmp_path.iterdir()
    }

    stats = collect_stats(tmp_path)
    format_stats(stats, directory=tmp_path, since=date(2026, 11, 1))

    after = {
        path.name: (path.stat().st_mtime_ns, path.read_bytes())
        for path in tmp_path.iterdir()
    }
    assert after == before


def test_t11_abandoned_is_its_own_bucket_and_does_not_change_success_rate(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "merged.json", _record("merged"))
    _write(
        tmp_path,
        "failed.json",
        _record(
            "failed",
            failure={"code": "verify_failed", "message": "x"},
        ),
    )
    _write(tmp_path, "abandoned-1.json", _record("abandoned"))
    _write(tmp_path, "abandoned-2.json", _record("abandoned"))

    stats = collect_stats(tmp_path)

    assert stats["abandoned"] == 2
    assert stats["failed"] == 1
    assert stats["in_flight"] == 0
    assert stats["by_state"]["abandoned"] == 2
    # 分母仍是 delivered + failed；abandon 既不加分也不拉低。
    assert stats["completion_rate"] == 0.5
    assert stats["success_rate"] == 0.5
    assert "abandoned   : 2" in format_stats(stats)
