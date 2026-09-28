"""工具调用契约测试（ADR-018 决策 1–3、5）。

这一层存在的理由：工具调用有两条路径（``ReActLoop`` 与 ``stubs._execute_with_tools``），
此前都不校验参数、且对 ``session_id`` 的处理还相反（一个 setdefault 可被模型压过，
另一个根本不注入）。契约测试钉住"只在一个地方决定模型给的参数能不能进 handler"。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.tool_contract import call_tool, validate_tool_args
from app.agents.tools import AgentTool

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}


def _search_tool(handler=None) -> AgentTool:
    async def default_handler(query: str) -> str:
        return f"hit:{query}"

    return AgentTool(
        name="knowledge_search",
        description="检索",
        parameters=_SEARCH_SCHEMA,
        handler=handler or default_handler,
    )


def _no_arg_tool() -> AgentTool:
    async def handler() -> str:
        return "ok"

    return AgentTool(
        name="current_time",
        description="时间",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


def _note_tool(seen: dict) -> AgentTool:
    async def handler(note: str, session_id: str = "") -> str:
        seen["session_id"] = session_id
        seen["note"] = note
        return "saved"

    return AgentTool(
        name="add_note",
        description="记笔记",
        parameters={
            "type": "object",
            "properties": {
                "note": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["note"],
        },
        handler=handler,
    )


# ── 校验 ────────────────────────────────────────────────────────────────────


def test_valid_args_pass() -> None:
    assert validate_tool_args(_search_tool(), {"query": "redis"}) is None


def test_missing_required_is_rejected() -> None:
    reason = validate_tool_args(_search_tool(), {})
    assert reason is not None and "query" in reason


def test_type_mismatch_is_rejected() -> None:
    reason = validate_tool_args(_search_tool(), {"query": 123})
    assert reason is not None and "query" in reason and "string" in reason


def test_undeclared_param_is_rejected() -> None:
    """未声明的参数必须拦下——否则 handler(**args) 会以 TypeError 收场，
    "模型填错"与"环境坏了"就落进同一个计数器。"""
    reason = validate_tool_args(_search_tool(), {"query": "x", "bogus": 1})
    assert reason is not None and "bogus" in reason


def test_no_arg_tool_rejects_any_param() -> None:
    """无参工具声明的是 "properties": {}，任何参数都该拒。"""
    reason = validate_tool_args(_no_arg_tool(), {"anything": 1})
    assert reason is not None and "anything" in reason


def test_broken_tool_schema_does_not_reject_the_model() -> None:
    """工具自己声明的 schema 非法是我们的 bug，不能把这次调用判成模型的错。"""
    tool = AgentTool(
        name="broken_schema",
        description="schema 写错了",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "definitely-not-a-real-type"}},
        },
        handler=_search_tool().handler,
    )
    assert validate_tool_args(tool, {"x": "v"}) is None


# ── 注入与执行 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_id_from_model_is_overwritten() -> None:
    """模型自带的 session_id 一律作废——这正是跨会话读写的入口。"""
    seen: dict = {}
    outcome = await call_tool(
        _note_tool(seen),
        {"note": "hi", "session_id": "attacker-session"},
        session_id="real-session",
    )
    assert outcome.kind == "ok"
    assert seen["session_id"] == "real-session"
    assert seen["note"] == "hi"


@pytest.mark.asyncio
async def test_session_id_not_injected_when_undeclared() -> None:
    """只给**声明**了该参数的工具注入；顺带：此时模型自带 session_id 会被判未声明。"""
    outcome = await call_tool(
        _search_tool(), {"query": "x", "session_id": "s"}, session_id="s"
    )
    assert outcome.kind == "rejected"


@pytest.mark.asyncio
async def test_rejected_does_not_call_handler() -> None:
    calls = {"n": 0}

    async def handler(query: str) -> str:
        calls["n"] += 1
        return "should not run"

    outcome = await call_tool(_search_tool(handler), {}, session_id="s")
    assert outcome.kind == "rejected"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_handler_exception_is_failed_not_rejected() -> None:
    async def boom(query: str) -> str:
        raise RuntimeError("tool down")

    outcome = await call_tool(_search_tool(boom), {"query": "x"}, session_id="s")
    assert outcome.kind == "failed" and "tool down" in outcome.detail


# ── calculator 上界与不阻塞（ADR-018 决策 5）────────────────────────────────


@pytest.mark.asyncio
async def test_calculator_rejects_exploding_pow() -> None:
    """9**9**9 若不拦，会在事件循环里同步算出天文数字级的整数。"""
    from app.agent_core.tools_extra import _calculator_handler

    assert "上限" in await _calculator_handler("9**9**9")


@pytest.mark.asyncio
async def test_calculator_rejects_chained_pow_beyond_bit_budget() -> None:
    """只限指数不够：(9**64)**64 的指数是 64，但结果是 9**4096。"""
    from app.agent_core.tools_extra import _calculator_handler

    assert "上限" in await _calculator_handler("(9**64)**64")


@pytest.mark.asyncio
async def test_calculator_rejects_overlong_expression() -> None:
    from app.agent_core.tools_extra import _calculator_handler

    assert "过长" in await _calculator_handler("1+" * 150 + "1")


@pytest.mark.asyncio
async def test_calculator_still_computes_normally() -> None:
    from app.agent_core.tools_extra import _calculator_handler

    assert (await _calculator_handler("2+2")).endswith("= 4")
    assert (await _calculator_handler("9**64")).startswith("9**64 = ")


@pytest.mark.asyncio
async def test_calculator_eval_runs_off_the_event_loop(monkeypatch) -> None:
    """纯 CPU 求值必须丢到线程里，不能在事件循环上同步跑。"""
    from app.agent_core import tools_extra

    used = {"n": 0}
    real_to_thread = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        used["n"] += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(tools_extra.asyncio, "to_thread", spy)
    assert (await tools_extra._calculator_handler("2+2")).endswith("= 4")
    assert used["n"] == 1
