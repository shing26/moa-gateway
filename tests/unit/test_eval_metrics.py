"""Agent 级指标：工具选择准确率（CI 硬门禁）、HITL 反馈装载、指标聚合。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.run_evals import (
    build_agent_metrics,
    load_dataset,
    load_hitl_feedback,
    run_e2e_eval,
    run_tool_selection_eval,
)

ROOT = Path(__file__).resolve().parents[2]
DATASETS = ROOT / "evals" / "datasets"


@pytest.mark.asyncio
async def test_shipped_tool_selection_dataset_is_perfect():
    """随包数据集必须 100% 命中：任何漂移都意味着规则/注册名变了却没同步用例。"""
    result = await run_tool_selection_eval(load_dataset(DATASETS / "tool_selection.jsonl"))
    assert result["misses"] == [], f"工具选择漂移: {result['misses']}"
    assert result["accuracy"] == 1.0
    assert result["total"] >= 15


@pytest.mark.asyncio
async def test_every_expected_tool_is_registered():
    """数据集里点名的工具必须真的注册在工具表里（防止用例与实现脱节）。"""
    from app.agents.tools import tool_registry
    import app.agent_core.tools_extra  # noqa: F401 注册扩展工具

    registered = {schema["function"]["name"] for schema in tool_registry.list_schemas()}
    expected = {
        str(case.get("expected_tool", ""))
        for case in load_dataset(DATASETS / "tool_selection.jsonl")
    } - {""}
    assert expected <= registered, f"用例引用了未注册的工具: {expected - registered}"


def test_load_hitl_feedback_reports_unavailable_without_dataset(tmp_path: Path):
    result = load_hitl_feedback(tmp_path)
    assert result["available"] is False
    assert result["cases"] == 0


def test_load_hitl_feedback_reads_cases_and_meta(tmp_path: Path):
    (tmp_path / "hitl_feedback.jsonl").write_text(
        "\n".join([
            json.dumps({"id": "hitl-1", "decision": "approve", "decision_latency_ms": 100.0}),
            json.dumps({"id": "hitl-2", "decision": "reject", "decision_latency_ms": 300.0}),
        ]),
        encoding="utf-8",
    )
    (tmp_path / "hitl_feedback.meta.json").write_text(
        json.dumps({"human_intervention_rate": 0.05, "requests": 40, "note": "seeded"}),
        encoding="utf-8",
    )
    result = load_hitl_feedback(tmp_path)
    assert result["available"] is True
    assert result["cases"] == 2
    assert result["approve_rate"] == 0.5
    assert result["avg_decision_latency_ms"] == 200.0
    assert result["human_intervention_rate"] == 0.05
    assert result["note"] == "seeded"


def test_load_hitl_feedback_flags_synthetic_seeds(tmp_path: Path):
    """模拟点击产生的决策必须被标出来。

    随包数据集当前全是本地模拟点击的种子（会话前缀 probe-*），那几个比率在统计上
    没有意义——报告与命令行摘要都要让人看得见这一点，而不是悄悄把 0.65% 当真实
    人工介入率讲。
    """
    (tmp_path / "hitl_feedback.jsonl").write_text(
        "\n".join([
            json.dumps({"id": "1", "decision": "approve", "session_id": "probe-seed-a-1"}),
            json.dumps({"id": "2", "decision": "reject", "session_id": "probe-seed-r-1"}),
            json.dumps({"id": "3", "decision": "approve", "session_id": "feishu-ou_abc"}),
        ]),
        encoding="utf-8",
    )

    result = load_hitl_feedback(tmp_path)

    assert result["cases"] == 3
    assert result["synthetic_cases"] == 2
    assert result["real_cases"] == 1


def test_agent_metrics_aggregates_the_three_sources():
    metrics = build_agent_metrics(
        {"success_rate": 0.9, "avg_cost_usd": 0.002, "avg_latency_ms": 1200.0},
        {"accuracy": 1.0},
        {"available": True, "human_intervention_rate": 0.02, "approve_rate": 0.8, "cases": 5},
    )
    assert metrics["task_success_rate"] == 0.9
    assert metrics["tool_selection_accuracy"] == 1.0
    assert metrics["human_intervention_rate"] == 0.02
    assert metrics["human_decisions"] == 5


def test_agent_metrics_marks_human_metrics_unknown_without_dataset():
    metrics = build_agent_metrics({"success_rate": 1.0}, {"accuracy": 1.0}, {"available": False})
    assert metrics["human_intervention_rate"] is None
    assert metrics["human_decisions"] == 0


@pytest.mark.asyncio
async def test_e2e_success_rate_counts_status_mismatches():
    """非 offline 路径的成功率：期望状态不符计失败（用假 pipeline/judge 离线验证）。"""
    from app.pipeline import PipelineResult

    class _Pipeline:
        async def run(self, event, *, channel, target):
            ok = PipelineResult(
                trace_id=event.trace_id, state="", intent="", text="ok", status="ok",
            )
            blocked = PipelineResult(
                trace_id=event.trace_id, state="", intent="", text="nope", status="blocked",
            )
            return blocked if "blocked" in event.text else ok

    async def _judge(_input: str, _output: str, _criteria: str) -> float:
        return 1.0

    cases = [
        {"id": "a", "input": "普通", "expected": {"status": "ok"}},
        {"id": "b", "input": "blocked 请求", "expected": {"status": "ok"}},
    ]
    result = await run_e2e_eval(cases, pipeline=_Pipeline(), judge=_judge, use_store=False)
    assert result["success_rate"] == 0.5
    assert result["run"] == 2


@pytest.mark.asyncio
async def test_e2e_dataset_can_expect_a_non_ok_status():
    """数据集能表达"期望被拦截"：expected.status 与真实状态一致即算通过。

    为什么失败路径不写进 e2e.jsonl：guard 的 evaluate_output 检查的是 **agent
    输出**而不是用户输入，活体路径下模型输出不可控，所以"输入含内网 IP 就必然
    blocked"并不成立——那样加进去只是 flaky 的门禁数据。失败路径的可复现覆盖在
    单测里（test_agent_retry.py / test_pipeline.py）。这条只钉住 harness 的表达
    能力，免得以后想加拦截用例时以为必须改 schema。
    """
    from app.pipeline import PipelineResult

    class _Pipeline:
        async def run(self, event, *, channel, target):
            return PipelineResult(
                trace_id=event.trace_id, state="", intent="",
                text="blocked by policy", status="blocked",
            )

    async def _judge(_input: str, _output: str, _criteria: str) -> float:
        return 0.8

    cases = [
        {"id": "b", "input": "含内网 IP 的请求", "expected": {"status": "blocked"}},
    ]
    result = await run_e2e_eval(cases, pipeline=_Pipeline(), judge=_judge, use_store=False)
    assert result["success_rate"] == 1.0
    assert result["avg_judge_score"] == 0.8
