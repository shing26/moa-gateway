"""§5.0 账本与预算的共享算术。"""

from __future__ import annotations

import pytest

from bench.agent.types import Budget, Usage


def test_usage_add_merges_every_counter() -> None:
    left = Usage(
        llm_calls=1,
        prompt_tokens=10,
        completion_tokens=4,
        cost_usd=0.25,
        llm_latency_ms=12.0,
    )
    right = Usage(
        llm_calls=2,
        prompt_tokens=20,
        completion_tokens=8,
        cost_usd=0.5,
        llm_latency_ms=24.0,
    )

    merged = left + right

    assert merged == Usage(
        llm_calls=3,
        prompt_tokens=30,
        completion_tokens=12,
        cost_usd=pytest.approx(0.75),
        llm_latency_ms=36.0,
    )


def test_budget_remaining_is_clamped_to_zero() -> None:
    remaining = Budget(1.0, 5).remaining(
        usage=Usage(cost_usd=1.2),
        steps_used=6,
    )

    assert remaining == Budget(0.0, 0)


def test_budget_stop_status_checks_cost_before_steps() -> None:
    budget = Budget(1.0, 5)
    assert budget.stop_status(usage=Usage(), steps_used=0) is None
    assert budget.stop_status(usage=Usage(), steps_used=5) == "step_limit"
    assert (
        budget.stop_status(usage=Usage(cost_usd=1.0), steps_used=5)
        == "budget_exhausted"
    )
