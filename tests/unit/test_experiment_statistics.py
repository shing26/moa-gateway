from __future__ import annotations

from dataclasses import replace

from bench.experiment.protocol import ProtocolStatistics
from bench.report import (
    RunRow,
    model_arm_matrix,
    paired_pass_rate_comparison,
    render_paired_markdown,
)


def _preregistration() -> ProtocolStatistics:
    return ProtocolStatistics(
        unit="task",
        primary_metric="pass_rate",
        candidate_arm="L3",
        baseline_arm="L0",
        planned_task_count=8,
        planned_repeats_per_cell=3,
        min_valid_repeats_per_cell=3,
        confidence_level=0.95,
        bootstrap_resamples=2_000,
        random_seed=42,
        alpha=0.05,
        sensitivity=("leave_one_task_out", "origin_stratified"),
    )


def _rows(
    *,
    candidate_outcomes: tuple[str, ...] = ("pass", "pass", "pass"),
    baseline_outcomes: tuple[str, ...] = ("fail", "fail", "fail"),
) -> list[RunRow]:
    rows: list[RunRow] = []
    for index in range(8):
        task_id = f"task-{index}"
        origin = "E1" if index < 5 else "E3"
        for outcome in candidate_outcomes:
            rows.append(
                RunRow(
                    task_id=task_id,
                    arm="L3",
                    outcome=outcome,
                    prompt_tokens=100,
                    completion_tokens=20,
                    model="model-a",
                    task_origin=origin,
                )
            )
        for outcome in baseline_outcomes:
            rows.append(
                RunRow(
                    task_id=task_id,
                    arm="L0",
                    outcome=outcome,
                    prompt_tokens=80,
                    completion_tokens=10,
                    model="model-a",
                    task_origin=origin,
                )
            )
    return rows


def test_paired_analysis_is_gated_by_preregistered_valid_samples() -> None:
    tasks = tuple(f"task-{index}" for index in range(8))
    comparison = paired_pass_rate_comparison(
        _rows(),
        _preregistration(),
        expected_tasks=tasks,
    )

    assert comparison.authorized is True
    assert comparison.decision == "candidate_better"
    assert comparison.mean_difference == 1.0
    assert comparison.confidence_interval == (1.0, 1.0)
    assert comparison.p_value == 2 / 256
    assert comparison.observed_task_count == 8
    assert comparison.sensitivity["origin_stratified"] == [
        {
            "task_origin": "E1",
            "tasks": 5,
            "mean_difference": 1.0,
            "inferential_claim": False,
        },
        {
            "task_origin": "E3",
            "tasks": 3,
            "mean_difference": 1.0,
            "inferential_claim": False,
        },
    ]
    markdown = render_paired_markdown(comparison)
    assert "分析门禁：**已授权" in markdown
    assert "显著差异声明：**已授权" in markdown
    assert "candidate_better" in markdown


def test_paired_analysis_zero_difference_is_not_a_significance_claim() -> None:
    comparison = paired_pass_rate_comparison(
        _rows(
            candidate_outcomes=("fail", "fail", "fail"),
            baseline_outcomes=("fail", "fail", "fail"),
        ),
        _preregistration(),
        expected_tasks=tuple(f"task-{index}" for index in range(8)),
    )

    assert comparison.authorized is True
    assert comparison.decision == "no_significant_difference"
    assert comparison.significant_difference is False
    markdown = render_paired_markdown(comparison)
    assert "分析门禁：**已授权" in markdown
    assert "显著差异声明：**不授权" in markdown


def test_paired_analysis_keeps_task_origin_when_all_runs_are_errors() -> None:
    rows = [replace(row, outcome="error") for row in _rows()]

    comparison = paired_pass_rate_comparison(
        rows,
        _preregistration(),
        expected_tasks=tuple(f"task-{index}" for index in range(8)),
    )

    tasks = {item.task_id: item for item in comparison.tasks}
    assert tasks["task-5"].task_origin == "E3"
    assert comparison.sensitivity["origin_stratified"] == []


def test_paired_analysis_uses_protocol_origin_for_missing_task() -> None:
    comparison = paired_pass_rate_comparison(
        [],
        _preregistration(),
        expected_tasks=("missing-e3",),
        expected_task_origins={"missing-e3": "E3"},
    )

    assert comparison.tasks[0].task_origin == "E3"


def test_paired_analysis_blocks_missing_origin_instead_of_defaulting_to_e1() -> None:
    comparison = paired_pass_rate_comparison(
        [],
        _preregistration(),
        expected_tasks=("missing-origin",),
    )

    assert comparison.authorized is False
    assert comparison.tasks[0].task_origin == ""
    assert any(
        "missing-origin 缺少任务来源" in reason
        for reason in comparison.block_reasons
    )


def test_paired_analysis_refuses_claim_when_any_cell_is_under_sampled() -> None:
    rows = _rows(
        baseline_outcomes=("fail", "fail", "error"),
    )
    tasks = tuple(f"task-{index}" for index in range(8))

    comparison = paired_pass_rate_comparison(
        rows,
        _preregistration(),
        expected_tasks=tasks,
    )

    assert comparison.authorized is False
    assert comparison.decision == "blocked"
    assert comparison.confidence_interval is None
    assert comparison.p_value is None
    assert comparison.significant_difference is False
    assert any("有效样本 2 < 3" in reason for reason in comparison.block_reasons)


def test_paired_analysis_blocks_unselected_cross_model_input() -> None:
    rows = _rows()
    rows[0] = RunRow(
        task_id=rows[0].task_id,
        arm=rows[0].arm,
        outcome=rows[0].outcome,
        model="model-b",
        task_origin=rows[0].task_origin,
    )

    comparison = paired_pass_rate_comparison(rows, _preregistration())

    assert comparison.authorized is False
    assert "必须显式选择一个模型" in comparison.block_reasons[0]


def test_model_matrix_does_not_treat_missing_cost_signal_as_zero() -> None:
    rows = _rows()
    rows.append(
        RunRow(
            task_id="task-paid",
            arm="L0",
            outcome="pass",
            model="model-paid",
            cost_usd=0.25,
        )
    )
    rows.append(
        RunRow(
            task_id="task-partial",
            arm="L0",
            outcome="pass",
            model="model-paid",
            cost_usd=0.0,
        )
    )

    matrix = {(item.model, item.arm): item for item in model_arm_matrix(rows)}

    assert matrix[("model-a", "L0")].cost_signal == "unavailable_or_zero"
    assert matrix[("model-paid", "L0")].cost_signal == "mixed_or_unpriced"
    assert matrix[("model-a", "L0")].pass_rate == 0.0
