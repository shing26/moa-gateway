"""预算策略（§5.3）：B1 等预算、B3 放宽复跑，两档产物可分辨。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.agent.types import AgentRun, Budget, RunResult, Usage
from bench.harness.budget import (
    B1_TIER,
    B3_TIER,
    BudgetPolicy,
    b3_candidates,
)
from bench.harness.runner import write_result


def _result(
    task_id: str,
    arm: str,
    outcome: str,
    *,
    tier: str = B1_TIER,
    cost: float = 0.0,
    baseline: float = 0.0,
) -> RunResult:
    run = AgentRun(
        status="completed",
        steps=(),
        usage=Usage(cost_usd=cost),
        model="m",
        temperature=0.0,
    )
    return RunResult(
        task_id=task_id,
        arm=arm,
        base_commit="abc",
        agent_run=run,
        outcome=outcome,  # type: ignore[arg-type]
        patch="",
        fail_first_exit_code=1,
        verdict_exit_code=0,
        verdict_output="",
        wall_clock_s=0.0,
        budget_tier=tier,
        baseline_cost_usd=baseline,
    )


def test_b1_is_one_budget_shared_by_every_tier() -> None:
    policy = BudgetPolicy(main_cost_usd=1.5, main_steps=30)

    assert policy.b1() == Budget(1.5, 30)
    assert policy.for_tier(B1_TIER) == policy.b1()


def test_b3_is_wider_than_b1_and_shares_the_token_fallback() -> None:
    policy = BudgetPolicy()

    assert policy.for_tier(B3_TIER).max_cost_usd > policy.b1().max_cost_usd
    assert policy.for_tier(B3_TIER).max_steps > policy.b1().max_steps
    assert policy.for_tier(B3_TIER).max_tokens == policy.b1().max_tokens


def test_unknown_tier_is_rejected() -> None:
    with pytest.raises(ValueError):
        BudgetPolicy().for_tier("B2")


def test_b3_candidates_take_failures_and_budget_exhausted_not_pass_or_error() -> None:
    results = [
        _result("t1", "L0", "pass"),
        _result("t1", "L3", "fail"),
        _result("t2", "L2", "budget_exhausted"),
        _result("t2", "L3", "error"),
        _result("t1", "L3", "fail"),  # 重复的 (task, arm) 只留一次
    ]

    assert b3_candidates(results) == (("t1", "L3"), ("t2", "L2"))


def test_b3_candidates_ignore_runs_that_are_already_b3() -> None:
    results = [_result("t1", "L3", "fail", tier=B3_TIER)]

    assert b3_candidates(results) == ()


def test_two_budget_tiers_are_distinguishable_in_the_artifact(
    tmp_path: Path,
) -> None:
    b1 = _result("t1", "L3", "fail", tier=B1_TIER, cost=0.4)
    b3 = _result("t1", "L3", "pass", tier=B3_TIER, cost=1.1, baseline=0.4)

    j1 = json.loads(
        write_result(b1, tmp_path / "b1.json").read_text(encoding="utf-8")
    )
    j3 = json.loads(
        write_result(b3, tmp_path / "b3.json").read_text(encoding="utf-8")
    )

    assert j1["budget_tier"] == "B1"
    assert j3["budget_tier"] == "B3"
    assert j3["baseline_cost_usd"] == pytest.approx(0.4)
    # 增量成本 = 本次 - B1 基线。
    assert b3.incremental_cost_usd == pytest.approx(0.7)
    assert b1.incremental_cost_usd == pytest.approx(0.4)


def test_token_cap_is_a_fallback_stop_not_the_main_criterion() -> None:
    over = Budget(max_cost_usd=100.0, max_steps=100, max_tokens=100)
    usage = Usage(prompt_tokens=60, completion_tokens=40)

    assert over.stop_status(usage=usage, steps_used=0) == "budget_exhausted"
    # 不设 token 上限（0）时它不拦——主线只判美元。
    unlimited = Budget(max_cost_usd=100.0, max_steps=100)
    assert unlimited.stop_status(usage=usage, steps_used=0) is None
    # 全局预算切段时 token 兜底跟着走。
    assert unlimited.remaining(usage=usage, steps_used=0).max_tokens == 0
