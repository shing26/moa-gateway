"""离线报告 CLI（``bench/report_curve.py``）：参数契约与目录/文件读取。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import report_curve as cli


def _write_run(
    directory: Path,
    name: str,
    *,
    task: str,
    arm: str,
    outcome: str,
    cost: float = 0.0,
    tier: str = "B1",
) -> Path:
    payload = {
        "task_id": task,
        "arm": arm,
        "outcome": outcome,
        "budget_tier": tier,
        "agent_run": {"model": "m", "usage": {"cost_usd": cost}},
    }
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_parse_args_defaults_to_b1_and_requires_runs() -> None:
    args = cli.parse_args(["--runs", "somewhere"])

    assert args.runs == ["somewhere"]
    assert args.tier == "B1"
    assert args.out == ""

    with pytest.raises(SystemExit):
        cli.parse_args([])


def test_collect_reads_a_directory_sorted_and_keeps_given_file_order(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_run(runs, "b.json", task="t1", arm="L1", outcome="fail")
    _write_run(runs, "a.json", task="t1", arm="L0", outcome="pass")
    single = _write_run(tmp_path, "z.json", task="t2", arm="L0", outcome="pass")

    # 目录按文件名排序 → 确定性；显式给的多个输入保持给定顺序。
    assert [row.source for row in cli._collect([str(runs)])] == ["a.json", "b.json"]
    assert [row.source for row in cli._collect([str(single), str(runs)])] == [
        "z.json",
        "a.json",
        "b.json",
    ]


def test_main_prints_the_curve_and_writes_the_aggregate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_run(runs, "t1-L0.json", task="t1", arm="L0", outcome="pass", cost=0.1)
    out = tmp_path / "report.json"

    code = cli.main(["--runs", str(runs), "--tier", "B1", "--out", str(out)])

    assert code == 0
    printed = capsys.readouterr().out
    assert "剂量曲线" in printed
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["tier"] == "B1"
    assert payload["total_runs"] == 1
    assert payload["sources"] == ["t1-L0.json"]
