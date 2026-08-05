from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.contract import AgentEnvelope, get_agent
from app.agents.provider import ChatResult, LLMClient, LLMConfig


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
def fake_llm() -> LLMClient:
    client = LLMClient(LLMConfig(api_key="test-key", base_url="http://localhost:0"))
    mock_post = AsyncMock()
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "def fib(n): return n if n <= 1 else fib(n-1) + fib(n-2)"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20},
        },
        raise_for_status=lambda: None,
    )
    client._client.post = mock_post
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
    fake_llm._client.post.assert_called_once()


@pytest.mark.asyncio
async def test_llm_client_handles_api_error() -> None:
    client = LLMClient(LLMConfig(api_key="bad-key", base_url="http://localhost:0"))
    mock_post = AsyncMock(side_effect=Exception("API error"))
    client._client.post = mock_post

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

    call_args = fake_llm._client.post.call_args
    payload = call_args.kwargs["json"]
    assert payload["tools"] == tools
    assert payload["stream"] is False
    assert payload["model"] == "gpt-4o-mini"
    assert payload["messages"] == original
    assert payload["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_llm_client_chat_with_tools_returns_tool_calls() -> None:
    client = LLMClient(LLMConfig(api_key="test-key", base_url="http://localhost:0"))
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "current_time", "arguments": "{}"},
        }
    ]
    assistant_message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
    mock_post = AsyncMock(
        return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{"message": assistant_message, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
            raise_for_status=lambda: None,
        )
    )
    client._client.post = mock_post

    result = await client.chat_with_tools([{"role": "user", "content": "what time is it"}], [])
    assert result.content == ""
    assert result.tool_calls == tool_calls
    assert len(result.messages) == 2
    assert result.messages[-1] == assistant_message
    payload = mock_post.call_args.kwargs["json"]
    assert payload["tools"] == []


@pytest.mark.asyncio
async def test_llm_client_chat_with_tools_handles_api_error() -> None:
    client = LLMClient(LLMConfig(api_key="bad-key", base_url="http://localhost:0"))
    mock_post = AsyncMock(side_effect=Exception("API error"))
    client._client.post = mock_post

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
