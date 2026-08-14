from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents.contract import AgentEnvelope, get_agent
from app.agents.provider import ChatResult, LLMClient, LLMConfig


def _make_message(content: Any, tool_calls: list[dict[str, Any]] | None = None) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _make_response(
    content: Any = "def fib(n): return n if n <= 1 else fib(n-1) + fib(n-2)",
    tool_calls: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    message = _make_message(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")],
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=20),
    )


def _patch_completion(monkeypatch: pytest.MonkeyPatch, mock_acompletion: AsyncMock) -> None:
    fake_module = SimpleNamespace(
        acompletion=mock_acompletion,
        completion_cost=lambda **kwargs: 0.0,
    )
    monkeypatch.setattr("app.agents.provider._get_litellm", lambda: fake_module)


@pytest.fixture
def envelope() -> AgentEnvelope:
    return AgentEnvelope(
        trace_id="test-trace",
        session_id="test-session",
        user_raw_input="\u5199\u4e00\u4e2a Python \u51fd\u6570\uff0c\u8ba1\u7b97\u6590\u6ce2\u7eb3\u5951\u6570\u5217",
        global_summary="\u7528\u6237\u6b63\u5728\u5b66\u4e60 Python",
        agent_local_slot={"language": "python"},
    )


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    client = LLMClient(LLMConfig(api_key="test-key", base_url="http://localhost:0"))
    mock_acompletion = AsyncMock(return_value=_make_response())
    _patch_completion(monkeypatch, mock_acompletion)
    client._completion_mock = mock_acompletion
    return client


@pytest.mark.asyncio
async def test_coder_agent_execute(envelope: AgentEnvelope, fake_llm: LLMClient) -> None:
    from app.agents.stubs import CoderAgent

    agent = CoderAgent(llm=fake_llm)
    result = await agent.execute(envelope)
    assert "fib" in result
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_general_agent_execute(envelope: AgentEnvelope, fake_llm: LLMClient) -> None:
    from app.agents.stubs import GeneralAgent

    agent = GeneralAgent(llm=fake_llm)
    result = await agent.execute(envelope)
    assert "fib" in result
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_agent_registry_contains_both() -> None:
    coder = get_agent("coder")
    general = get_agent("general")
    assert coder is not None
    assert general is not None


@pytest.mark.asyncio
async def test_llm_client_chat_formats_request(fake_llm: LLMClient) -> None:
    result = await fake_llm.chat([{"role": "user", "content": "hello"}])
    assert isinstance(result, str)
    assert len(result) > 0
    fake_llm._completion_mock.assert_called_once()


@pytest.mark.asyncio
async def test_llm_client_handles_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LLMClient(LLMConfig(api_key="bad-key", base_url="http://localhost:0"))
    mock_acompletion = AsyncMock(side_effect=Exception("API error"))
    _patch_completion(monkeypatch, mock_acompletion)

    with pytest.raises(Exception, match="API error"):
        await client.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_llm_client_chat_with_tools_payload_and_plain_content(fake_llm: LLMClient) -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "current_time",
                "description": "get current time",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    original = [{"role": "user", "content": "what time is it"}]
    result = await fake_llm.chat_with_tools(original, tools)
    assert isinstance(result, ChatResult)
    assert result.content == "def fib(n): return n if n <= 1 else fib(n-1) + fib(n-2)"
    assert result.tool_calls == []
    assert len(result.messages) == 2
    assert result.messages[0] == original[0]
    assert result.messages[-1]["role"] == "assistant"
    assert result.messages[-1]["content"] == result.content
    assert original == [{"role": "user", "content": "what time is it"}]

    call_args = fake_llm._completion_mock.call_args
    payload = call_args.kwargs
    assert payload["tools"] == tools
    assert payload["stream"] is False
    assert payload["model"] == "gpt-4o-mini"
    assert payload["messages"] == original
    assert payload["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_llm_client_chat_with_tools_returns_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LLMClient(LLMConfig(api_key="test-key", base_url="http://localhost:0"))
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "current_time", "arguments": "{}"},
        }
    ]
    mock_acompletion = AsyncMock(return_value=_make_response(content=None, tool_calls=tool_calls))
    _patch_completion(monkeypatch, mock_acompletion)

    result = await client.chat_with_tools([{"role": "user", "content": "what time is it"}], [])
    assert result.content == ""
    assert result.tool_calls == tool_calls
    assert len(result.messages) == 2
    assert result.messages[-1]["content"] is None
    payload = mock_acompletion.call_args.kwargs
    assert payload["tools"] == []


@pytest.mark.asyncio
async def test_llm_client_chat_with_tools_handles_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LLMClient(LLMConfig(api_key="bad-key", base_url="http://localhost:0"))
    mock_acompletion = AsyncMock(side_effect=Exception("API error"))
    _patch_completion(monkeypatch, mock_acompletion)

    with pytest.raises(Exception, match="API error"):
        await client.chat_with_tools([{"role": "user", "content": "hi"}], [])


@pytest.mark.asyncio
async def test_general_agent_uses_runtime_env(monkeypatch, envelope: AgentEnvelope) -> None:
    import os

    from app.agents.stubs import GeneralAgent

    seen: list[str] = []

    class FakeClient:
        def __init__(self, config: LLMConfig) -> None:
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def chat(self, messages, **kwargs):
            return self.config.model

    def make_client():
        model = os.environ.get("LLM_MODEL", "")
        seen.append(model)
        return FakeClient(LLMConfig(model=model))

    monkeypatch.setattr("app.agents.stubs._default_llm", make_client)
    saved = os.environ.get("LLM_MODEL")
    try:
        os.environ["LLM_MODEL"] = "runtime-model-a"
        first = await GeneralAgent().execute(envelope)
        os.environ["LLM_MODEL"] = "runtime-model-b"
        second = await GeneralAgent().execute(envelope)
        assert first == "runtime-model-a"
        assert second == "runtime-model-b"
        assert seen == ["runtime-model-a", "runtime-model-b"]
    finally:
        if saved is None:
            os.environ.pop("LLM_MODEL", None)
        else:
            os.environ["LLM_MODEL"] = saved
