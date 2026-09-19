from __future__ import annotations

import pytest

from app.agent_core.task_agent import TaskAgent
from app.agents.contract import AgentEnvelope, get_agent
from app.agents import loader  # noqa: F401  triggers agent registration


def test_task_agent_registered_in_registry() -> None:
    agent = get_agent("task")
    assert agent is not None
    assert isinstance(agent, TaskAgent)


@pytest.mark.asyncio
async def test_task_agent_execute_writes_plan_and_results() -> None:
    agent = get_agent("task")
    assert agent is not None

    envelope = AgentEnvelope(
        trace_id="trace-test",
        session_id="web:unit-task",
        user_raw_input="帮我算 3*7 并且现在几点",
        global_summary="",
        agent_local_slot={},
    )
    answer = await agent.execute(envelope)

    assert "任务完成报告" in answer
    assert "帮我算 3*7" in answer or "3*7" in answer
    plan = envelope.agent_local_slot.get("plan")
    assert isinstance(plan, list) and len(plan) >= 1
    task_results = envelope.agent_local_slot.get("task_results")
    assert isinstance(task_results, list)
    assert envelope.agent_local_slot.get("tool_calls_total", 0) >= 1


@pytest.mark.asyncio
async def test_task_agent_execute_notes_roundtrip() -> None:
    agent = get_agent("task")
    assert agent is not None
    sid = "web:unit-notes"
    from app.agent_core.tools_extra import _NOTES_STORE

    _NOTES_STORE.pop(sid, None)
    envelope = AgentEnvelope(
        trace_id="trace-notes",
        session_id=sid,
        user_raw_input="记录一下 记得买牛奶 并且 查看笔记",
        global_summary="",
        agent_local_slot={},
    )
    answer = await agent.execute(envelope)
    assert "买牛奶" in answer
    assert envelope.agent_local_slot.get("tool_calls_total", 0) >= 1
    _NOTES_STORE.pop(sid, None)
