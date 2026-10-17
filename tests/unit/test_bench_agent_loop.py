"""§5.0 循环：四个结局，以及"完成优先于预算"这条顺序。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bench.agent.loop import LLMBackedAgent
from bench.agent.scripted import ScriptedLLM, ScriptedTurn
from bench.agent.tools import build_toolset
from bench.agent.types import Budget

FAILING_VERIFY = (sys.executable, "-c", "raise SystemExit(1)")


def make_agent(workspace: Path, turns: list[ScriptedTurn]) -> LLMBackedAgent:
    tools = build_toolset(workspace=workspace, verify_command=FAILING_VERIFY)
    return LLMBackedAgent(ScriptedLLM(turns), tools)


class _FallbackScriptedLLM(ScriptedLLM):
    """``config.model`` 说主模型，``last_metrics["model_used"]`` 说另一个——模拟
    fallback 链接手：真实作答的是后者，账本必须记后者。"""

    async def chat_with_tools(self, messages, tools, **kwargs):  # type: ignore[override]
        result = await super().chat_with_tools(messages, tools, **kwargs)
        self.last_metrics["model_used"] = "openrouter/fallback-model"
        return result


@pytest.mark.asyncio
async def test_terminal_turn_is_completed(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, [ScriptedTurn(final="做完了")])
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))
    assert run.status == "completed"
    assert run.steps[-1].action == "finish"
    assert run.usage.llm_calls == 1


@pytest.mark.asyncio
async def test_running_out_of_steps_is_step_limit(tmp_path: Path) -> None:
    turns = [ScriptedTurn(tool="list_files", arguments={}) for _ in range(4)]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(10.0, 3))
    assert run.status == "step_limit"
    assert run.usage.llm_calls == 3


@pytest.mark.asyncio
async def test_cost_over_budget_is_budget_exhausted(tmp_path: Path) -> None:
    turns = [
        ScriptedTurn(tool="list_files", arguments={}, cost_usd=2.0),
        ScriptedTurn(final="本可以做完"),
    ]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 10))
    assert run.status == "budget_exhausted"
    assert run.usage.cost_usd == 2.0
    # 越额的那一轮没有执行工具：硬停就是不执行。
    assert run.steps == ()


@pytest.mark.asyncio
async def test_llm_failure_is_error(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, [ScriptedTurn(error="boom")])
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))
    assert run.status == "error"
    assert "boom" in run.error


@pytest.mark.asyncio
async def test_rejected_tool_becomes_observation_and_loop_continues(
    tmp_path: Path,
) -> None:
    turns = [
        ScriptedTurn(tool="read_file", arguments={"path": "../outside.txt"}),
        ScriptedTurn(final="换个做法"),
    ]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))
    assert run.status == "completed"
    assert run.steps[0].outcome == "rejected"
    assert "工作区" in run.steps[0].observation


@pytest.mark.asyncio
async def test_unknown_tool_is_failed_not_rejected(tmp_path: Path) -> None:
    turns = [ScriptedTurn(tool="no_such_tool"), ScriptedTurn(final="好")]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))
    assert run.steps[0].outcome == "failed"


@pytest.mark.asyncio
async def test_completion_wins_over_budget_in_the_same_round(tmp_path: Path) -> None:
    """模型刚好在预算越顶的那一轮交了终局：它确实完成了，不能记成预算耗尽。"""
    turns = [
        ScriptedTurn(tool="list_files", arguments={}, cost_usd=0.4),
        ScriptedTurn(final="完成", cost_usd=0.9),
    ]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 10))
    assert run.status == "completed"
    assert run.usage.cost_usd > 1.0


@pytest.mark.asyncio
async def test_reported_model_is_the_one_that_answered(tmp_path: Path) -> None:
    """账本记的是**实际作答**的模型，不是配置里的主模型。"""
    tools = build_toolset(workspace=tmp_path, verify_command=FAILING_VERIFY)
    agent = LLMBackedAgent(
        _FallbackScriptedLLM([ScriptedTurn(final="完成")], model="primary-model"), tools
    )
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))
    assert run.model == "openrouter/fallback-model"


@pytest.mark.asyncio
async def test_token_cap_is_a_budget_fallback(tmp_path: Path) -> None:
    """token 兜底命中同样归 budget_exhausted，且越额那一轮不执行工具。"""
    turns = [
        ScriptedTurn(
            tool="list_files",
            arguments={},
            prompt_tokens=80,
            completion_tokens=40,
        ),
        ScriptedTurn(final="本可以做完"),
    ]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(
        workspace=tmp_path, goal="目标", budget=Budget(100.0, 10, max_tokens=100)
    )
    assert run.status == "budget_exhausted"
    assert run.usage.total_tokens == 120
    assert run.steps == ()
