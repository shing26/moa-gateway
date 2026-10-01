"""按 task_id 查审计行，以及脚本的种类校验（D4 判据的读取端）。

判据原文："``verify_audit_chain.py`` 按 task_id 捞出 7 条且链完整"。这里只测读取端；
哈希链本身由 tests/unit/test_audit.py 覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.audit.recorder import entries_for_trace

TASK_ID = "cr_o/r#42@abc1234"


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def test_entries_are_filtered_by_trace_id(tmp_path: Path) -> None:
    _write(
        tmp_path / "audit-2026-10-01.jsonl",
        [
            {"trace_id": TASK_ID, "agent_name": "triage"},
            {"trace_id": "other", "agent_name": "triage"},
            {"trace_id": TASK_ID, "agent_name": "report"},
        ],
    )
    rows = entries_for_trace(tmp_path, TASK_ID)
    assert [r["agent_name"] for r in rows] == ["triage", "report"]


def test_entries_span_multiple_daily_files(tmp_path: Path) -> None:
    """任务可能跨过零点：WAL 按天分文件，查询不能只看当天。"""
    _write(tmp_path / "audit-2026-10-01.jsonl", [{"trace_id": TASK_ID, "agent_name": "a"}])
    _write(tmp_path / "audit-2026-10-02.jsonl", [{"trace_id": TASK_ID, "agent_name": "b"}])
    assert len(entries_for_trace(tmp_path, TASK_ID)) == 2


def test_corrupt_line_is_skipped_not_raised(tmp_path: Path) -> None:
    """手工改坏的审计行不该让查询工具整个挂掉——发现它正是链校验的职责。"""
    path = tmp_path / "audit-2026-10-01.jsonl"
    path.write_text(
        json.dumps({"trace_id": TASK_ID, "agent_name": "triage"})
        + "\n{ not json\n"
        + json.dumps({"trace_id": TASK_ID, "agent_name": "report"})
        + "\n",
        encoding="utf-8",
    )
    rows = entries_for_trace(tmp_path, TASK_ID)
    assert [r["agent_name"] for r in rows] == ["triage", "report"]


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert entries_for_trace(tmp_path / "nope", TASK_ID) == []


def test_script_names_the_missing_agent(tmp_path: Path, capsys) -> None:
    """只写 4 条 agent：种类校验必须指出缺了哪个，而不是只报"条数不符"。

    只数条数的话，"5 条 lifecycle + 2 条 agent"也能凑够 7，于是"某个 agent
    根本没跑"这类缺陷就查不出来——而那正是最该被发现的情况。
    """
    from scripts.verify_audit_chain import main

    names = ("triage", "static_analysis", "semantic_review", "test_coverage")
    rows: list[dict[str, object]] = [
        {"trace_id": TASK_ID, "agent_name": n, "agent_output": ""} for n in names
    ]
    rows.append({"trace_id": TASK_ID, "agent_name": "task_lifecycle", "agent_output": "claim"})
    _write(tmp_path / "audit-2026-10-01.jsonl", rows)

    code = main(["--logs-dir", str(tmp_path), "--task", TASK_ID])
    err = capsys.readouterr().err
    assert code == 1
    assert "report" in err, "缺 report 行时应指名道姓"


def test_script_passes_when_all_kinds_present(tmp_path: Path) -> None:
    from scripts.verify_audit_chain import main

    from apps.code_review_pipeline.task_audit import AGENT_NAMES, LIFECYCLE_AGENT

    rows: list[dict[str, object]] = [
        {"trace_id": TASK_ID, "agent_name": n, "agent_output": ""} for n in AGENT_NAMES
    ]
    rows.append({"trace_id": TASK_ID, "agent_name": LIFECYCLE_AGENT, "agent_output": "claim"})
    rows.append({"trace_id": TASK_ID, "agent_name": "human_decision", "agent_output": "ok"})
    _write(tmp_path / "audit-2026-10-01.jsonl", rows)

    assert main(["--logs-dir", str(tmp_path), "--task", TASK_ID, "--quiet"]) == 0


def test_script_fails_when_task_absent(tmp_path: Path) -> None:
    from scripts.verify_audit_chain import main

    _write(tmp_path / "audit-2026-10-01.jsonl", [{"trace_id": "other"}])
    assert main(["--logs-dir", str(tmp_path), "--task", TASK_ID]) == 1
