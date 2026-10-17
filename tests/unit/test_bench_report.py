"""剂量曲线聚合（票 08）：任务 × 档位表、边际收益、结局分列、确定性。"""

from __future__ import annotations

import json
from pathlib import Path

from bench.report import (
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
    # 通过率只看 pass；budget_exhausted / error 不混进 fail。
    assert cell.pass_rate == 0.25
    assert cell.avg_cost_usd == 0.25


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
