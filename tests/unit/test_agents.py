from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.contract import AgentEnvelope, get_agent
from app.agents.provider import LLMClient, LLMConfig


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
                    "message": {"content": "def fib(n): return n if n <= 1 else fib(n-1) + fib(n-2)"},
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
