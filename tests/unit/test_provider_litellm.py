from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.provider import LLMClient, LLMConfig


def _make_response(content: str = "ok") -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def _patch_litellm(
    monkeypatch: pytest.MonkeyPatch,
    mock_acompletion: AsyncMock,
    cost: float = 0.0,
) -> None:
    fake_module = SimpleNamespace(
        acompletion=mock_acompletion,
        completion_cost=lambda **kwargs: cost,
    )
    monkeypatch.setattr("app.agents.provider._get_litellm", lambda: fake_module)


@pytest.mark.asyncio
async def test_chat_forwards_expected_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_acompletion = AsyncMock(return_value=_make_response())
    _patch_litellm(monkeypatch, mock_acompletion)
    client = LLMClient(LLMConfig(api_key="key", base_url="http://localhost:0", model="model-a"))

    result = await client.chat(
        [{"role": "user", "content": "hello"}],
        max_tokens=128,
        temperature=0.2,
    )

    assert result == "ok"
    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["model"] == "model-a"
    assert kwargs["messages"] == [{"role": "user", "content": "hello"}]
    assert kwargs["max_tokens"] == 128
    assert kwargs["temperature"] == 0.2
    assert kwargs["stream"] is False
    assert kwargs["api_key"] == "key"
    assert kwargs["api_base"] == "http://localhost:0"


@pytest.mark.asyncio
async def test_chat_with_tools_forwards_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_acompletion = AsyncMock(return_value=_make_response())
    _patch_litellm(monkeypatch, mock_acompletion)
    client = LLMClient(LLMConfig(model="model-a"))
    tools = [{"type": "function", "function": {"name": "current_time"}}]

    result = await client.chat_with_tools([{"role": "user", "content": "now"}], tools)

    assert result.content == "ok"
    assert mock_acompletion.call_args.kwargs["tools"] == tools


@pytest.mark.asyncio
async def test_fallback_model_is_used_after_primary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_acompletion = AsyncMock(
        side_effect=[Exception("primary down"), _make_response(content="fallback ok")]
    )
    _patch_litellm(monkeypatch, mock_acompletion)
    config = LLMConfig(
        api_key="key",
        base_url="http://localhost:0",
        model="primary-model",
        fallback_models=["fallback-model"],
    )
    client = LLMClient(config)

    result = await client.chat([{"role": "user", "content": "hi"}])

    assert result == "fallback ok"
    assert mock_acompletion.call_count == 2
    assert client.last_metrics["model_used"] == "fallback-model"
    assert client.last_metrics["fallback_used"] == "fallback-model"


@pytest.mark.asyncio
async def test_all_models_failed_raises_last_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_acompletion = AsyncMock(side_effect=Exception("all down"))
    _patch_litellm(monkeypatch, mock_acompletion)
    config = LLMConfig(
        api_key="key",
        base_url="http://localhost:0",
        model="primary-model",
        fallback_models=["fallback-model"],
    )
    client = LLMClient(config)

    with pytest.raises(Exception, match="all down"):
        await client.chat([{"role": "user", "content": "hi"}])

    assert mock_acompletion.call_count == 2


@pytest.mark.asyncio
async def test_cost_is_recorded_from_response(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_acompletion = AsyncMock(return_value=_make_response())
    _patch_litellm(monkeypatch, mock_acompletion, cost=0.012345)
    client = LLMClient(LLMConfig(model="gpt-4o-mini"))

    await client.chat([{"role": "user", "content": "hi"}])

    assert client.last_metrics["cost_usd"] == 0.012345
    assert client.last_metrics["prompt_tokens"] == 10
    assert client.last_metrics["completion_tokens"] == 5
    assert client.last_metrics["llm_latency_ms"] >= 0


def test_from_env_tolerates_empty_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MAX_TOKENS", "")
    monkeypatch.setenv("LLM_TIMEOUT", "")
    monkeypatch.setenv("LLM_TEMPERATURE", "")
    monkeypatch.delenv("LLM_FALLBACK_MODELS", raising=False)

    config = LLMConfig.from_env()

    assert config.max_tokens == 4096
    assert config.timeout == 120.0
    assert config.temperature == 0.7
    assert config.fallback_models == []


def test_from_env_parses_fallback_models(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin provider to `direct` so fallback models stay unqualified and this test
    # asserts the pure parsing path. Without this pin, LLM_PROVIDER from .env
    # (omniroute, an openai-compatible provider) leaks in via load_dotenv() at
    # import time and _qualify_model() prepends "openai/", breaking the assertion.
    monkeypatch.setenv("LLM_PROVIDER", "direct")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", " model-b , model-c ,,")

    config = LLMConfig.from_env()

    assert config.fallback_models == ["model-b", "model-c"]


@pytest.mark.asyncio
async def test_bare_model_with_custom_base_url_gets_custom_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_acompletion = AsyncMock(return_value=_make_response())
    _patch_litellm(monkeypatch, mock_acompletion)
    client = LLMClient(LLMConfig(
        api_key="key",
        base_url="http://localhost:20128/v1",
        model="high-availability",
        provider="omniroute",
    ))

    await client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["model"] == "high-availability"
    assert kwargs["custom_llm_provider"] == "openai"


def test_from_env_qualifies_bare_model_by_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "deepseek-reasoner")

    config = LLMConfig.from_env()

    assert config.model == "deepseek/deepseek-chat"
    assert config.fallback_models == ["deepseek/deepseek-reasoner"]


def test_from_env_omniroute_uses_openai_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "omniroute")
    monkeypatch.setenv("LLM_MODEL", "high-availability")

    config = LLMConfig.from_env()

    assert config.model == "openai/high-availability"


@pytest.mark.asyncio
async def test_bare_model_infers_provider_from_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_acompletion = AsyncMock(return_value=_make_response())
    _patch_litellm(monkeypatch, mock_acompletion)
    client = LLMClient(LLMConfig(
        api_key="key",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
    ))

    await client.chat([{"role": "user", "content": "hi"}])

    assert mock_acompletion.call_args.kwargs["custom_llm_provider"] == "deepseek"


def test_from_env_local_uses_openai_compatible_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("LLM_API_KEY", "ollama-local")

    config = LLMConfig.from_env()

    assert config.provider == "local"
    assert config.model == "qwen2.5:7b"
    assert config.base_url == "http://localhost:11434/v1"
    assert config.api_key == "ollama-local"


@pytest.mark.asyncio
async def test_colon_model_gets_openai_custom_provider_for_local(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_acompletion = AsyncMock(return_value=_make_response())
    _patch_litellm(monkeypatch, mock_acompletion)
    client = LLMClient(LLMConfig(
        api_key="ollama-local",
        base_url="http://localhost:11434/v1",
        model="qwen2.5:7b",
        provider="local",
    ))

    await client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["model"] == "qwen2.5:7b"
    assert kwargs["custom_llm_provider"] == "openai"
    assert kwargs["api_base"] == "http://localhost:11434/v1"
