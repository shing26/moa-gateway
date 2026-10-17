"""§5.0 的续跑缝：一次循环可被再次推进，且后一段看得见前一段的历史。

L1 的有界回边靠这条缝——Critic 的修正指令加进**同一条**会话，而不是另起一次
``run()`` 把前文丢掉。
"""

from __future__ import annotations

from typing import Any

import pytest

from bench.agent.loop import LLMBackedAgent, LoopSession
from bench.agent.scripted import ScriptedLLM, ScriptedTurn
from bench.agent.tools import build_toolset
from bench.agent.types import Budget

FAILING_VERIFY = ("python", "-c", "raise SystemExit(1)")


class _RecordingLLM(ScriptedLLM):
    """记下每次调用收到的 messages，用来断言历史真的延续了。"""

    def __init__(self, turns: list[ScriptedTurn]) -> None:
        super().__init__(turns)
        self.seen: list[list[dict[str, Any]]] = []

    async def chat_with_tools(self, messages, tools, **kwargs):  # type: ignore[override]
        self.seen.append([dict(message) for message in messages])
        return await super().chat_with_tools(messages, tools, **kwargs)


def _session(tmp_path, turns, verify=FAILING_VERIFY) -> LoopSession:
    tools = build_toolset(workspace=tmp_path, verify_command=verify)
    return LoopSession(
        LLMBackedAgent(_RecordingLLM(turns), tools),
        workspace=tmp_path,
        goal="目标",
        budget=Budget(max_cost_usd=5.0, max_steps=5),
    )


@pytest.mark.asyncio
async def test_a_completed_session_can_be_continued_with_injected_text(tmp_path) -> None:
    session = _session(
        tmp_path,
        [ScriptedTurn(final="第一段做完了"), ScriptedTurn(final="按指令又做了一步")],
    )

    first = await session.advance()
    assert first == "completed"
    assert len(session.steps) == 0  # 终局步由调用方补，会话自己不加

    session.add_user_message("请继续修改。")
    second = await session.advance()

    assert second == "completed"
    # 第二次调用看得见第一段与刚注入的指令——这正是"同一条会话"的意思。
    last_call = session._runner._llm.seen[-1]
    joined = "\n".join(str(message.get("content", "")) for message in last_call)
    assert "第一段做完了" in joined
    assert "请继续修改。" in joined


@pytest.mark.asyncio
async def test_step_allowance_is_per_advance_over_a_shared_ceiling(tmp_path) -> None:
    session = _session(
        tmp_path,
        [
            ScriptedTurn(tool="list_files", arguments={}),
            ScriptedTurn(tool="list_files", arguments={}),
            ScriptedTurn(tool="list_files", arguments={}),
            ScriptedTurn(tool="list_files", arguments={}),
            ScriptedTurn(tool="list_files", arguments={}),
            ScriptedTurn(tool="list_files", arguments={}),
        ],
    )

    assert await session.advance(max_turns=2) == "step_limit"
    assert session.turns == 2
    # 全局上限（Budget.max_steps=5）是这条会话唯一的天花板：续跑接着算，不从零重来。
    assert await session.advance(max_turns=5) == "step_limit"
    assert session.turns == 5


@pytest.mark.asyncio
async def test_continue_reports_budget_exhaustion_after_a_carrying_call(tmp_path) -> None:
    tools = build_toolset(workspace=tmp_path, verify_command=FAILING_VERIFY)
    session = LoopSession(
        LLMBackedAgent(
            ScriptedLLM(
                [
                    ScriptedTurn(tool="list_files", arguments={}, cost_usd=1.0),
                    ScriptedTurn(tool="list_files", arguments={}, cost_usd=1.0),
                ]
            ),
            tools,
        ),
        workspace=tmp_path,
        goal="目标",
        budget=Budget(max_cost_usd=2.0, max_steps=5),
    )

    assert await session.advance(max_turns=1) == "step_limit"
    # 续跑接着累计：两段加起来 $2.0 达到上限，硬停且不执行工具。
    assert await session.advance(max_turns=5) == "budget_exhausted"
    assert session.usage.cost_usd == pytest.approx(2.0)
