from __future__ import annotations

import pytest

from app.agent_core.mock_llm import MockTaskLLM


@pytest.mark.asyncio
async def test_plan_splits_by_connectors() -> None:
    llm = MockTaskLLM()
    plan = await llm.plan(task="搜索一下 python 并且 计算 3*5")
    assert len(plan) == 2


@pytest.mark.asyncio
async def test_plan_returns_single_when_no_separator() -> None:
    llm = MockTaskLLM()
    plan = await llm.plan(task="帮我算 2+2")
    assert len(plan) == 1
    assert "2+2" in plan[0]


@pytest.mark.asyncio
async def test_decide_matches_calculator_rule() -> None:
    llm = MockTaskLLM()
    decision = await llm.decide(task="帮我算 3*7", subtask="帮我算 3*7", observations=[])
    assert decision.action == "call_tool"
    assert decision.tool_name == "calculator"
    assert decision.arguments.get("expression") == "3*7"


@pytest.mark.asyncio
async def test_decide_matches_time_rule() -> None:
    llm = MockTaskLLM()
    decision = await llm.decide(task="现在几点", subtask="现在几点", observations=[])
    assert decision.action == "call_tool"
    assert decision.tool_name == "current_time"


@pytest.mark.asyncio
async def test_decide_finishes_after_observation() -> None:
    llm = MockTaskLLM()
    decision = await llm.decide(
        task="帮我算 2+2", subtask="帮我算 2+2",
        observations=["工具 calculator(...) => 2+2 = 4"],
    )
    assert decision.action == "finish"
    assert "2+2 = 4" in decision.final_answer


@pytest.mark.asyncio
async def test_decide_generic_reply_when_no_rule() -> None:
    llm = MockTaskLLM()
    decision = await llm.decide(task="随便聊聊", subtask="随便聊聊", observations=[])
    assert decision.action == "finish"
    assert "已收到你的请求" in decision.final_answer


@pytest.mark.asyncio
async def test_summarize_includes_plan_and_steps() -> None:
    from app.agent_core.types import ReActDecision, TaskResult

    llm = MockTaskLLM()
    result = TaskResult(
        answer="结果 A",
        steps=[],
        plan=["步骤 1"],
        tool_calls=2,
    )
    report = await llm.summarize(
        task="任务 X", plan=["步骤 1"], results=[result],
    )
    assert "任务完成报告" in report
    assert "任务 X" in report
    assert "步骤 1" in report
    assert "工具调用" in report
