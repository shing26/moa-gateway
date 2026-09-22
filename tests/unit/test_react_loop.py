from __future__ import annotations

import pytest

from app.agent_core.types import ReActDecision, TaskResult, TaskStep


@pytest.mark.asyncio
async def test_react_loop_runs_tool_then_finish() -> None:
    from app.agent_core.mock_llm import MockTaskLLM
    from app.agent_core.react import ReActLoop
    from app.agents.tools import ToolRegistry, AgentTool

    async def current_time_handler() -> str:
        return "2026-09-02T10:00:00+08:00"

    registry = ToolRegistry()
    registry.register(
        AgentTool(
            name="current_time",
            description="get current time",
            parameters={"type": "object", "properties": {}},
            handler=current_time_handler,
        )
    )

    loop = ReActLoop(MockTaskLLM(), registry, max_steps=8, session_id="s1")
    result = await loop.run(task="现在几点", subtask="现在几点")

    assert result.tool_calls == 1
    assert len(result.steps) == 2
    assert result.steps[0].decision.action == "call_tool"
    assert result.steps[0].decision.tool_name == "current_time"
    assert "2026-09-02" in result.answer
    assert result.steps[1].decision.action == "finish"


@pytest.mark.asyncio
async def test_react_loop_max_steps_bounded() -> None:
    from app.agent_core.react import ReActLoop
    from app.agent_core.types import ReActDecision
    from app.agents.tools import ToolRegistry, AgentTool

    calls = {"n": 0}

    async def always_tool() -> str:
        calls["n"] += 1
        return "obs"

    class NeverFinishLLM:
        async def decide(self, *, task, subtask, observations):
            return ReActDecision(action="call_tool", tool_name="ping", note="keep going")

        async def plan(self, *, task):
            return [task]

        async def summarize(self, *, task, plan, results):
            return "done"

    registry = ToolRegistry()
    registry.register(
        AgentTool(
            name="ping",
            description="ping",
            parameters={"type": "object", "properties": {}},
            handler=always_tool,
        )
    )

    loop = ReActLoop(NeverFinishLLM(), registry, max_steps=3, session_id="s1")
    result = await loop.run(task="x", subtask="x")

    assert calls["n"] == 3
    assert result.tool_calls == 3
    assert "已达最大步数上限" in result.answer


@pytest.mark.asyncio
async def test_react_loop_missing_tool_reports_error() -> None:
    from app.agent_core.mock_llm import MockTaskLLM
    from app.agent_core.react import ReActLoop
    from app.agents.tools import ToolRegistry

    registry = ToolRegistry()
    loop = ReActLoop(MockTaskLLM(), registry, max_steps=8, session_id="s1")

    # 强制决策调用不存在的工具
    from app.agent_core.types import ReActDecision

    class CallMissingTool:
        async def decide(self, *, task, subtask, observations):
            if observations:
                return ReActDecision(action="finish", final_answer="")
            return ReActDecision(action="call_tool", tool_name="nope", note="try")

        async def plan(self, *, task):
            return [task]

        async def summarize(self, *, task, plan, results):
            return "done"

    loop._llm = CallMissingTool()
    result = await loop.run(task="x", subtask="x")

    assert result.tool_calls == 0
    assert result.tool_errors == 1
    assert "工具不存在" in result.answer


@pytest.mark.asyncio
async def test_react_loop_counts_failed_tool_calls() -> None:
    """工具抛异常被降级成 observation（有意设计），但必须留下计数。

    否则"任务真的完成"与"所有工具都失败但优雅降级"在审计里长得一模一样——
    上层无法区分这两者（2026-09-22 外部评估指出的可观测性缺口）。
    """
    from app.agent_core.react import ReActLoop
    from app.agents.tools import AgentTool, ToolRegistry

    async def boom() -> str:
        raise RuntimeError("tool down")

    registry = ToolRegistry()
    registry.register(
        AgentTool(
            name="broken",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            handler=boom,
        )
    )

    class CallBroken:
        async def decide(self, *, task, subtask, observations):
            if observations:
                return ReActDecision(action="finish", final_answer="")
            return ReActDecision(action="call_tool", tool_name="broken", note="try")

        async def plan(self, *, task):
            return [task]

        async def summarize(self, *, task, plan, results):
            return "done"

    loop = ReActLoop(CallBroken(), registry, max_steps=8, session_id="s1")
    result = await loop.run(task="x", subtask="x")

    assert result.tool_calls == 0
    assert result.tool_errors == 1, "失败的调用必须计数，否则与'完成'无法区分"
    assert "调用失败" in result.answer


@pytest.mark.asyncio
async def test_react_loop_injects_session_id_to_tools() -> None:
    from app.agent_core.react import ReActLoop
    from app.agent_core.types import ReActDecision
    from app.agents.tools import ToolRegistry, AgentTool

    seen: dict[str, str] = {}

    async def note_handler(note: str, session_id: str = "") -> str:
        seen["session_id"] = session_id
        return "saved"

    class AddNoteLLM:
        async def decide(self, *, task, subtask, observations):
            if observations:
                return ReActDecision(action="finish", final_answer="ok")
            return ReActDecision(
                action="call_tool", tool_name="add_note", arguments={"note": "hi"}
            )

        async def plan(self, *, task):
            return [task]

        async def summarize(self, *, task, plan, results):
            return "ok"

    registry = ToolRegistry()
    registry.register(
        AgentTool(
            name="add_note",
            description="add note",
            parameters={
                "type": "object",
                "properties": {
                    "note": {"type": "string"},
                    "session_id": {"type": "string"},
                },
                "required": ["note"],
            },
            handler=note_handler,
        )
    )

    loop = ReActLoop(AddNoteLLM(), registry, max_steps=8, session_id="session-abc")
    result = await loop.run(task="记一下 hi", subtask="记一下 hi")

    assert seen.get("session_id") == "session-abc"
    assert result.tool_calls == 1
