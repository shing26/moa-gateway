"""§5.1 专家：与 L0 共用同一条工具循环、同一套工具、同一份契约，只多一个角色身份。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.agents.tools import AgentTool, ToolRegistry

from bench.agent.expert import (
    EXPERT_KEYS,
    EXPERTS,
    ExpertPool,
    UnknownExpertError,
)
from bench.agent.loop import LLMBackedAgent
from bench.agent.scripted import ScriptedLLM, ScriptedTurn
from bench.agent.tools import build_toolset
from bench.agent.types import Budget
from bench.harness.runner import load_task, run_task

ROOT = Path(__file__).resolve().parents[2]
TASK_FILE = ROOT / "bench" / "tasks" / "988fccb.task.json"
FIX_COMMIT = "988fccbb242be0573f4e42f05118276884272bf3"
FIXED_FILE = "app/middleware/auth.py"
FAILING_VERIFY = (sys.executable, "-c", "raise SystemExit(1)")


def _commit_available(sha: str) -> bool:
    probe = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", sha], capture_output=True, check=False
    )
    return probe.returncode == 0


def _show(pathspec: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", pathspec],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


requires_history = pytest.mark.skipif(
    not _commit_available(FIX_COMMIT),
    reason="需要完整克隆：浅克隆里没有基线提交",
)


def make_pool(workspace: Path, turns: list[ScriptedTurn], **kwargs: Any) -> ExpertPool:
    return ExpertPool(llm=ScriptedLLM(turns), tools=failing_tools(workspace), **kwargs)


def failing_tools(workspace: Path) -> ToolRegistry:
    return build_toolset(workspace=workspace, verify_command=FAILING_VERIFY)


class _RecordingLLM(ScriptedLLM):
    """记下每次调用收到的 messages，好断言角色指令真的进了 system prompt。"""

    def __init__(self, turns: list[ScriptedTurn]) -> None:
        super().__init__(turns)
        self.seen: list[list[dict[str, Any]]] = []

    async def chat_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs: Any
    ) -> Any:
        self.seen.append([dict(message) for message in messages])
        return await super().chat_with_tools(messages, tools, **kwargs)


def test_expert_keys_are_the_three_collaboration_roles() -> None:
    assert EXPERT_KEYS == ("coder", "general", "review")


def test_expert_vocabulary_matches_the_collaboration_chain() -> None:
    """专家词汇与协作链路的 ``EXPERT_KEYS`` 同源——同一个概念不另起一套名字。

    这里**故意**在测试里 import 协作模块：bench 运行期不 import 它（会把 langgraph
    拖进实验台的依赖面），但词汇漂移要有人守。langgraph 是可选 extra，没装时跳过。
    """
    collaboration = pytest.importorskip(
        "app.orchestration.collaboration", reason="langgraph 是可选 extra；未装时跳过"
    )
    assert EXPERT_KEYS == collaboration.EXPERT_KEYS


def test_expert_reuses_the_shared_agent_loop(tmp_path: Path) -> None:
    """§5.1 要求"有可复用的循环就复用，不另起一套"：专家内部就是 §5.0 的循环。

    这条断言钉住的正是那句排查结论——有人再写一个专家循环时它会红。
    """
    expert = make_pool(tmp_path, []).get("coder")
    assert isinstance(expert._agent, LLMBackedAgent)


def test_pool_rejects_an_unknown_expert(tmp_path: Path) -> None:
    """未登记的专家要报错，而不是静默回落成某个默认角色。"""
    pool = make_pool(tmp_path, [])
    with pytest.raises(UnknownExpertError):
        pool.get("planner")


def test_pool_temperature_can_be_overridden(tmp_path: Path) -> None:
    pool = make_pool(tmp_path, [], temperature=0.7)
    assert pool.temperature == pytest.approx(0.7)


def test_pool_temperature_falls_back_to_the_client_config(tmp_path: Path) -> None:
    pool = ExpertPool(
        llm=ScriptedLLM([], temperature=0.4),
        tools=failing_tools(tmp_path),
    )
    assert pool.temperature == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_expert_runs_on_the_toolset_the_harness_injects(tmp_path: Path) -> None:
    """专家取工具的唯一来源是构造期注入的 registry——它拿不到第二套工具。"""

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
    pool = ExpertPool(
        llm=ScriptedLLM([ScriptedTurn(tool="sentinel"), ScriptedTurn(final="好")]),
        tools=tools,
    )

    run = await pool.get("coder").run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))

    assert [step.tool for step in run.steps if step.action == "call_tool"] == ["sentinel"]
    assert run.steps[0].outcome == "ok"
    assert run.steps[0].observation == "pong"


@pytest.mark.asyncio
async def test_expert_usage_lands_in_the_shared_ledger_attributable_to_it(
    tmp_path: Path,
) -> None:
    """专家的轨迹与用量走的是同一个 ``AgentRun`` 账本，且系在具体专家名下。"""
    pool = make_pool(
        tmp_path,
        [ScriptedTurn(tool="list_files", arguments={}, cost_usd=0.03),
         ScriptedTurn(final="完成", cost_usd=0.02)],
    )

    run = await pool.get("review").run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))

    assert run.expert == "review"
    assert run.status == "completed"
    assert run.usage.llm_calls == 2
    assert run.usage.cost_usd == pytest.approx(0.05)
    assert [step.action for step in run.steps] == ["call_tool", "finish"]


@pytest.mark.asyncio
async def test_expert_carries_its_role_instructions_into_the_system_prompt(
    tmp_path: Path,
) -> None:
    llm = _RecordingLLM([ScriptedTurn(final="完成")])
    pool = ExpertPool(llm=llm, tools=failing_tools(tmp_path))

    await pool.get("coder").run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))

    system = str(llm.seen[0][0]["content"])
    assert EXPERTS["coder"].instructions in system
    assert "目标" in system


@pytest.mark.asyncio
async def test_rejected_and_failed_tools_degrade_to_observations(
    tmp_path: Path,
) -> None:
    """工具被拒 / 执行失败都只回流成观察，不中断专家循环。"""
    turns = [
        ScriptedTurn(tool="read_file", arguments={"path": "../outside.txt"}),
        ScriptedTurn(tool="no_such_tool"),
        ScriptedTurn(final="换个做法"),
    ]
    pool = make_pool(tmp_path, turns)

    run = await pool.get("general").run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))

    kinds = [step.outcome for step in run.steps if step.action == "call_tool"]
    assert kinds == ["rejected", "failed"]
    assert run.status == "completed"
    assert run.steps[-1].action == "finish"


@pytest.mark.asyncio
@requires_history
async def test_scripted_expert_writes_a_real_fix_and_verification_passes(
    tmp_path: Path,
) -> None:
    """收工判据：一个专家独立把失败测试从红改到绿，轨迹与用量落在同一份账本上。"""
    fixed_content = _show(f"{FIX_COMMIT}:{FIXED_FILE}")

    def factory(*, workspace: Path, tools: ToolRegistry) -> Any:
        turns = [
            ScriptedTurn(
                tool="write_file",
                arguments={"path": FIXED_FILE, "content": fixed_content},
                cost_usd=0.001,
            ),
            ScriptedTurn(final="已修好", cost_usd=0.001),
        ]
        return ExpertPool(llm=ScriptedLLM(turns), tools=tools).get("coder")

    result = await run_task(
        spec=load_task(TASK_FILE),
        agent_factory=factory,
        repo=ROOT,
        workdir=tmp_path / "work",
        budget=Budget(max_cost_usd=1.0, max_steps=10),
    )

    assert result.outcome == "pass", result.verdict_output
    assert result.agent_run.status == "completed"
    assert result.agent_run.expert == "coder"
    assert result.agent_run.usage.cost_usd == pytest.approx(0.002)
    assert [step.tool for step in result.agent_run.steps if step.action == "call_tool"] == [
        "write_file"
    ]
    assert result.patch_touched_tests is False
    assert "_DASH_PROTECTED_PREFIXES" in result.patch
