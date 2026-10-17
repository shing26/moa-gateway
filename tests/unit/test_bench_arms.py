"""arm 注册表：四档"哪些可用"的单一来源（§4.2 ``bench/arms/``）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.agents.tools import AgentTool, ToolRegistry

from bench.agent.scripted import ScriptedLLM, ScriptedTurn
from bench.agent.tools import build_toolset
from bench.agent.types import Budget
from bench.arms import UnknownArmError, arm_names, get_arm, resolve_arm

FAILING_VERIFY = (sys.executable, "-c", "raise SystemExit(1)")


def test_l0_is_registered_under_its_name() -> None:
    assert get_arm("L0").name == "L0"


def test_arm_names_lists_l0() -> None:
    assert "L0" in arm_names()


def test_unknown_arm_raises_instead_of_falling_back_to_l0() -> None:
    with pytest.raises(UnknownArmError):
        get_arm("L9")


@pytest.mark.asyncio
async def test_registry_builds_an_agent_that_runs_through_the_protocol(
    tmp_path: Path,
) -> None:
    """按名字取到的 L0 能顶替 harness 的 agent 位置一路跑完。"""
    factory = resolve_arm("L0", llm=ScriptedLLM([ScriptedTurn(final="完成")]))
    tools = build_toolset(workspace=tmp_path, verify_command=FAILING_VERIFY)

    agent = factory(workspace=tmp_path, tools=tools)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))

    assert run.status == "completed"


@pytest.mark.asyncio
async def test_l0_runs_on_the_toolset_the_harness_injects(tmp_path: Path) -> None:
    """四档共用同一套工具：arm 只在构造期拿到 harness 的 registry，取不到也别无来源。"""

    async def sentinel() -> str:
        return "pong"

    tools = ToolRegistry()
    tools.register(
        AgentTool(
            name="sentinel",
            description="只在测试里存在的探针工具",
            parameters={"type": "object", "properties": {}},
            handler=sentinel,
        )
    )
    factory = resolve_arm(
        "L0",
        llm=ScriptedLLM([ScriptedTurn(tool="sentinel"), ScriptedTurn(final="好")]),
    )

    run = await factory(workspace=tmp_path, tools=tools).run(
        workspace=tmp_path, goal="目标", budget=Budget(1.0, 5)
    )

    assert [step.tool for step in run.steps if step.action == "call_tool"] == ["sentinel"]
    assert run.steps[0].outcome == "ok"
    assert run.steps[0].observation == "pong"
