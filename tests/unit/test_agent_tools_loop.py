from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.agents.contract import AgentEnvelope
from app.agents.provider import ChatResult
from app.vectordb.retriever import RetrievalResult


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class FakeToolLLM:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResult:
        self.calls += 1
        resp = self.responses.pop(0)
        if callable(resp):
            content = resp(messages)
            msg = {"role": "assistant", "content": content}
            return ChatResult(messages=[*messages, msg], content=content, tool_calls=[])
        if isinstance(resp, str):
            msg = {"role": "assistant", "content": resp}
            return ChatResult(messages=[*messages, msg], content=resp, tool_calls=[])
        msg = {"role": "assistant", "content": None, "tool_calls": resp}
        return ChatResult(messages=[*messages, msg], content="", tool_calls=resp)


@pytest.fixture
def envelope() -> AgentEnvelope:
    return AgentEnvelope(
        trace_id="trace-1",
        session_id="sess-1",
        user_raw_input="帮我查一下 redis 的配置",
        global_summary="测试摘要",
        agent_local_slot={},
    )


@pytest.mark.asyncio
async def test_agent_executes_tool_and_returns_final_text(envelope, monkeypatch) -> None:
    fake_retriever = AsyncMock()
    fake_retriever.retrieve_knowledge = AsyncMock(
        return_value=RetrievalResult(chunks=["doc a"], context="doc a", doc_count=1)
    )
    monkeypatch.setattr("app.deps._retriever", fake_retriever)

    from app.agents.stubs import CoderAgent

    llm = FakeToolLLM(
        [
            [_tool_call("call_1", "knowledge_search", {"query": "redis config"})],
            "根据文档: 来源文档数: 1\n\ndoc a",
        ]
    )
    result = await CoderAgent(llm=llm).execute(envelope)
    assert "来源文档数" in result
    assert "doc a" in result
    assert llm.calls == 2
    fake_retriever.retrieve_knowledge.assert_awaited_once_with("redis config")


@pytest.mark.asyncio
async def test_tool_loop_caps_at_three_rounds(envelope) -> None:
    from app.agents.stubs import GeneralAgent

    llm = FakeToolLLM(
        [
            [_tool_call("c1", "current_time", {})],
            [_tool_call("c2", "current_time", {})],
            [_tool_call("c3", "current_time", {})],
            [_tool_call("c4", "current_time", {})],
        ]
    )
    result = await GeneralAgent(llm=llm).execute(envelope)
    assert llm.calls == 4
    assert "current_time" in result
    assert "工具调用已达上限" in result


@pytest.mark.asyncio
async def test_execute_code_marker_passes_through_tool_loop(envelope) -> None:
    from app.agents.stubs import CoderAgent

    llm = FakeToolLLM(
        [
            [_tool_call("c1", "execute_code", {"code": "print('hi')"})],
            lambda messages: messages[-1]["content"],
        ]
    )
    result = await CoderAgent(llm=llm).execute(envelope)
    assert "EXECUTION_REQUIRES_APPROVAL" in result
    assert "lines=1" in result
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_tool_loop_handles_bad_arguments(envelope, monkeypatch) -> None:
    fake_retriever = AsyncMock()
    fake_retriever.retrieve_knowledge = AsyncMock(
        return_value=RetrievalResult(chunks=["ok"], context="ok", doc_count=1)
    )
    monkeypatch.setattr("app.deps._retriever", fake_retriever)

    from app.agents.stubs import GeneralAgent

    llm = FakeToolLLM(
        [
            [
                {
                    "id": "bad_1",
                    "type": "function",
                    "function": {"name": "knowledge_search", "arguments": "not-json"},
                }
            ],
            lambda messages: messages[-1]["content"],
        ]
    )
    result = await GeneralAgent(llm=llm).execute(envelope)
    assert llm.calls == 2
    assert "error" in result
