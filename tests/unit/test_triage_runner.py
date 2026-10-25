from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.agent.types import AgentRun, Budget, Step, Usage
from bench.triage import (
    TriageTask,
    load_triage_task,
    run_triage_task,
    verify_triage_output,
)
from bench.experiment import load_protocol

TASKS_DIR = (
    Path(__file__).resolve().parents[2] / "bench" / "triage" / "tasks"
)


def _task() -> TriageTask:
    return load_triage_task(TASKS_DIR / "t1-crash-on-startup.triage.json")


def test_load_triage_task_validates_closed_sets() -> None:
    task = _task()
    assert task.expected_category == "bug"
    assert task.expected_priority == "P0"
    assert task.expected_route == "auto"
    assert len(task.issue_text) > 50


def test_load_triage_task_rejects_unknown_label() -> None:
    payload = json.loads(
        (TASKS_DIR / "t1-crash-on-startup.triage.json").read_text(
            encoding="utf-8"
        )
    )
    payload["expected_category"] = "chore"
    bad = TASKS_DIR / "_bad.triage.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="不在闭集内"):
            load_triage_task(bad)
    finally:
        bad.unlink()


def test_verify_accepts_exact_match() -> None:
    task = _task()
    text = '最终结论：{"category": "bug", "priority": "P0", "route": "auto"}'
    passed, verdict = verify_triage_output(text, task)
    assert passed is True
    assert verdict["checks"] == {
        "category": True,
        "priority": True,
        "route": True,
    }


def test_verify_rejects_partial_match() -> None:
    task = _task()
    text = '{"category": "bug", "priority": "P1", "route": "auto"}'
    passed, verdict = verify_triage_output(text, task)
    assert passed is True
    assert verdict["checks"]["priority"] is False
    assert verdict["decision_fields"] == ("category", "route")


def test_verify_rejects_unparseable_output() -> None:
    passed, verdict = verify_triage_output("我认为这是 bug", _task())
    assert passed is False
    assert "error" in verdict


def test_triage_verdict_semantics_are_identical_across_v1_and_v3() -> None:
    """runbook、v1、v3 共用同一判定实现：category+route 判决，priority 仅记录。"""
    root = TASKS_DIR.parents[1] / "protocols"
    for name in (
        "issue-triage-v1-local.json",
        "issue-triage-v3-l0-local.json",
    ):
        protocol = load_protocol(root / name)
        assert protocol.scope is None or protocol.scope.decision_fields == (
            "category",
            "route",
        )
        assert protocol.scope is None or protocol.scope.recorded_only == (
            "priority",
        )
    for path in sorted(TASKS_DIR.glob("*.triage.json")):
        task = load_triage_task(path)
        passed, verdict = verify_triage_output(
            f'{{"category": "{task.expected_category}", "priority": "WRONG", '
            f'"route": "{task.expected_route}"}}',
            task,
        )
        assert passed is True
        assert verdict["checks"]["priority"] is False


@pytest.mark.asyncio
async def test_run_triage_task_uses_finish_observation(tmp_path) -> None:
    task = _task()

    class FinishAgent:
        async def run(self, *, workspace, goal, budget) -> AgentRun:
            assert "issue 分诊" in goal or "Issue 分诊" in goal
            return AgentRun(
                status="completed",
                steps=(
                    Step(
                        index=1,
                        action="finish",
                        observation='{"category": "bug", "priority": "P0", "route": "auto"}',
                    ),
                ),
                usage=Usage(),
                model="mock",
                temperature=0.0,
            )

    result = await run_triage_task(
        task=task,
        agent_factory=lambda **kwargs: FinishAgent(),
        workdir=tmp_path,
        budget=Budget(max_cost_usd=1.0, max_steps=5),
    )
    assert result.outcome == "pass"
    assert result.parsed_output == {
        "category": "bug",
        "priority": "P0",
        "route": "auto",
    }


@pytest.mark.asyncio
async def test_run_triage_task_maps_agent_error(tmp_path) -> None:
    task = _task()

    class ErrorAgent:
        async def run(self, *, workspace, goal, budget) -> AgentRun:
            return AgentRun(
                status="error",
                steps=(),
                usage=Usage(),
                model="mock",
                temperature=0.0,
                error="boom",
            )

    result = await run_triage_task(
        task=task,
        agent_factory=lambda **kwargs: ErrorAgent(),
        workdir=tmp_path,
        budget=Budget(max_cost_usd=1.0, max_steps=5),
    )
    assert result.outcome == "error"
