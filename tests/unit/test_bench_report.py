"""剂量曲线聚合（票 08）：任务 × 档位表、边际收益、结局分列、确定性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.report import (
    MIN_RUNS_PER_CELL,
    RunRow,
    build_report,
    load_rows_dir,
    render_markdown,
)


def _write_run(
    directory: Path,
    name: str,
    *,
    task: str,
    arm: str,
    outcome: str,
    cost: float = 0.0,
    tier: str = "B1",
    model: str = "m",
) -> Path:
    """落一份 ``RunResult`` 形状的 JSON——聚合器只读它的落盘字段。"""
    payload = {
        "task_id": task,
        "arm": arm,
        "base_commit": "abc",
        "outcome": outcome,
        "budget_tier": tier,
        "patch": "",
        "fail_first_exit_code": 1,
        "verdict_exit_code": 0,
        "verdict_output": "",
        "wall_clock_s": 1.0,
        "patch_touched_tests": False,
        "baseline_cost_usd": 0.0,
        "agent_run": {
            "status": "completed",
            "steps": [],
            "usage": {
                "llm_calls": 1,
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "cost_usd": cost,
                "llm_latency_ms": 0.0,
            },
            "model": model,
            "temperature": 0.0,
        },
    }
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _row(
    task: str, arm: str, outcome: str, cost: float = 0.0, tier: str = "B1"
) -> RunRow:
    return RunRow(
        task_id=task, arm=arm, outcome=outcome, cost_usd=cost, budget_tier=tier
    )


def test_report_is_a_task_by_arm_table_with_gaps_for_missing_cells() -> None:
    report = build_report(
        [
            _row("t1", "L0", "pass", 0.10),
            _row("t1", "L1", "fail", 0.20),
            _row("t2", "L0", "pass", 0.30),
        ]
    )

    assert report.tasks == ("t1", "t2")
    # 四档即使有的没样本也要在列里——缺了就是缺口。
    assert report.arms == ("L0", "L1", "L2", "L3")
    assert report.cell("t1", "L0").pass_rate == 1.0
    assert report.cell("t1", "L1").pass_rate == 0.0
    assert report.cell("t1", "L2").runs == 0
    assert report.cell("t1", "L2").pass_rate is None
    assert ("t1", "L2") in report.gaps


def test_outcomes_are_counted_separately_not_folded_into_fail() -> None:
    report = build_report(
        [
            _row("t1", "L3", "pass", 0.10),
            _row("t1", "L3", "fail", 0.20),
            _row("t1", "L3", "budget_exhausted", 0.30),
            _row("t1", "L3", "error", 0.40),
        ]
    )

    cell = report.cell("t1", "L3")
    assert cell.outcomes == {
        "pass": 1,
        "fail": 1,
        "budget_exhausted": 1,
        "error": 1,
    }
    # 通过率的分母去掉 error（§8：error 是噪音，单独统计、不进通过率）；
    # budget_exhausted 留在分母里，但它仍单独成列，不并进 fail。
    assert cell.valid_runs == 3
    assert cell.pass_rate == 1 / 3
    assert cell.avg_cost_usd == 0.25


def test_all_error_cell_has_no_pass_rate() -> None:
    report = build_report([_row("t1", "L0", "error", 0.10)])

    cell = report.cell("t1", "L0")
    # 没有有效样本：通过率是 None（不是 0）——0 会被读成"跑了但没过"。
    assert cell.valid_runs == 0
    assert cell.pass_rate is None


def test_marginal_benefit_is_each_column_minus_the_previous() -> None:
    report = build_report(
        [
            _row("t1", "L0", "fail", 0.10),
            _row("t2", "L0", "fail", 0.10),
            _row("t1", "L1", "pass", 0.20),
            _row("t2", "L1", "fail", 0.20),
        ]
    )

    step = next(item for item in report.marginals if item.arm == "L1")
    assert step.previous_arm == "L0"
    # L0 通过率 0%，L1 通过率 50% → +50pp；平均成本各 +0.10。
    assert step.delta_pass_rate == 0.5
    assert step.delta_avg_cost_usd is not None
    assert abs(step.delta_avg_cost_usd - 0.10) < 1e-9

    # L2 没样本 → 相对 L1 的边际是 None（缺口，不是 0）。
    gap_step = next(item for item in report.marginals if item.arm == "L2")
    assert gap_step.delta_pass_rate is None


def test_tier_filter_keeps_b3_out_of_the_b1_report() -> None:
    rows = [
        _row("t1", "L3", "fail", 0.10, tier="B1"),
        _row("t1", "L3", "pass", 5.00, tier="B3"),
    ]

    b1 = build_report(rows, tier="B1")
    b3 = build_report(rows, tier="B3")

    assert b1.cell("t1", "L3").runs == 1
    assert b1.cell("t1", "L3").pass_rate == 0.0
    assert b3.cell("t1", "L3").pass_rate == 1.0


def test_same_input_recomputes_identically(tmp_path: Path) -> None:
    _write_run(tmp_path, "a.json", task="t1", arm="L0", outcome="pass", cost=0.1)
    _write_run(tmp_path, "b.json", task="t1", arm="L1", outcome="fail", cost=0.2)
    _write_run(tmp_path, "c.json", task="t2", arm="L0", outcome="pass", cost=0.3)

    rows = load_rows_dir(tmp_path)
    first = build_report(rows)
    second = build_report(rows)

    assert first.to_dict() == second.to_dict()
    assert render_markdown(first) == render_markdown(second)
    # 报告 JSON 必须能序列化——离线归档要能再读回来。
    json.dumps(first.to_dict(), ensure_ascii=False)


def test_loader_reads_the_run_result_json_from_disk(tmp_path: Path) -> None:
    _write_run(tmp_path, "run.json", task="t9", arm="L2", outcome="pass", cost=0.42)

    rows = load_rows_dir(tmp_path)

    assert len(rows) == 1
    assert rows[0].task_id == "t9"
    assert rows[0].arm == "L2"
    assert rows[0].cost_usd == 0.42
    assert rows[0].total_tokens == 15


def test_single_sample_report_says_it_is_not_a_curve() -> None:
    report = build_report([_row("t1", "L0", "pass", 0.1)])

    assert report.is_single_sample is True
    rendered = render_markdown(report)
    assert "单点样本" in rendered
    # 结局分布四列都在，且 budget_exhausted / error 没被并进 fail。
    assert "budget_exhausted" in rendered
    assert "| error |" in rendered


def test_run_row_from_dict_tolerates_missing_usage() -> None:
    row = RunRow.from_dict({"task_id": "t1", "arm": "L0", "outcome": "pass"})

    assert row.cost_usd == 0.0
    assert row.budget_tier == "B1"


def test_from_dict_rejects_an_unknown_budget_tier() -> None:
    # 静默收下一个不认识的档位会让它从曲线里"消失"又不报错——宁可炸。
    with pytest.raises(ValueError):
        RunRow.from_dict(
            {"task_id": "t1", "arm": "L0", "outcome": "pass", "budget_tier": "B9"}
        )


def test_report_records_the_source_run_files_for_traceability() -> None:
    # "报告里每个数字都能追到具体的 run"：来源文件名随 report 一起落盘。
    rows = [
        RunRow(task_id="t1", arm="L0", outcome="pass", source="t1-L0-1.json"),
        RunRow(task_id="t1", arm="L1", outcome="fail", source="t1-L1-1.json"),
    ]

    report = build_report(rows)

    assert report.sources == ("t1-L0-1.json", "t1-L1-1.json")
    assert report.to_dict()["sources"] == ["t1-L0-1.json", "t1-L1-1.json"]


def test_loader_attaches_the_file_name_as_the_source(tmp_path: Path) -> None:
    _write_run(tmp_path, "t1-L0-1.json", task="t1", arm="L0", outcome="pass")

    rows = load_rows_dir(tmp_path)

    assert rows[0].source == "t1-L0-1.json"


def test_declared_tasks_that_have_no_runs_show_up_as_gaps() -> None:
    # 只给出现过 task 的缺口会漏掉"整个任务一条都没跑"——显式声明任务集就能标出来。
    report = build_report(
        [_row("t1", "L0", "pass", 0.1)],
        tasks=["t1", "t2"],
    )

    assert report.tasks == ("t1", "t2")
    assert ("t2", "L0") in report.gaps
    assert ("t2", "L3") in report.gaps


def test_under_sampled_cells_are_flagged_below_the_three_run_bar() -> None:
    # §9 要求每个格子 3 次；1 次甚至混在别的满样本格子里也要被点出来。
    report = build_report(
        [_row("t1", "L0", "pass", 0.1), _row("t2", "L0", "pass", 0.1)]
        + [_row("t2", "L0", "pass", 0.1) for _ in range(MIN_RUNS_PER_CELL - 1)]
    )

    assert ("t1", "L0") in report.under_sampled
    assert ("t2", "L0") not in report.under_sampled
    assert "样本不足" in render_markdown(report)


def test_b3_report_reports_incremental_cost_over_b1_baseline() -> None:
    rows = [
        RunRow(
            task_id="t1",
            arm="L3",
            outcome="pass",
            cost_usd=1.1,
            budget_tier="B3",
            baseline_cost_usd=0.4,
        )
    ]

    report = build_report(rows, tier="B3")
    column = next(item for item in report.columns if item.arm == "L3")

    assert column.total_cost_usd == pytest.approx(1.1)
    assert column.total_incremental_cost_usd == pytest.approx(0.7)
    assert "增量成本" in render_markdown(report)
