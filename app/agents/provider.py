from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("moa.agents.provider")


def _get_litellm() -> Any:
    import litellm

    return litellm


_LITELLM_PROVIDERS = {
    "openai": "openai",
    "deepseek": "deepseek",
    "anthropic": "anthropic",
    "gemini": "gemini",
    "mistral": "mistral",
    "cohere": "cohere",
    "openrouter": "openrouter",
}

_OPENAI_COMPATIBLE_PROVIDERS = {
    "omniroute",
    "vllm",
    "hosted_vllm",
    "lmstudio",
    "openai_compatible",
    "nvidia",
    "nvidia_nim",
}


def _qualify_model(model: str, provider: str) -> str:
    """Prefix a bare model name with its LiteLLM provider when known."""
    if not model or "/" in model or ":" in model:
        return model
    provider = provider.lower()
    if provider in _LITELLM_PROVIDERS:
        return f"{_LITELLM_PROVIDERS[provider]}/{model}"
    if provider in _OPENAI_COMPATIBLE_PROVIDERS:
        return f"openai/{model}"
    if provider in ("local", "ollama"):
        return f"openai/{model}"
    return model


def _env_str(name: str, default: str) -> str:
    """Read an env var, treating blank values as "unset" so defaults apply.

    ``os.getenv(name, default)`` returns ``""`` for ``NAME=``, which would
    silently produce an empty base_url/provider. Blank means unset here, the
    same convention ``app.config`` uses for its numeric settings.
    """
    value = (os.getenv(name) or "").strip()
    return value or default


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout: float = 120.0
    max_tokens: int = 4096
    temperature: float = 0.7
    provider: str = "direct"
    extra_headers: dict[str, str] = field(default_factory=dict)
    fallback_models: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, prefix: str = "LLM") -> LLMConfig:
        key = prefix.upper()
        provider = _env_str(f"{key}_PROVIDER", "direct").lower()
        base_url = _env_str(f"{key}_BASE_URL", "https://api.openai.com/v1")
        api_key = os.getenv(f"{key}_API_KEY", "") or os.getenv("OMNIROUTE_API_KEY", "")
        model = _env_str(f"{key}_MODEL", "gpt-4o-mini")
        timeout = float(os.getenv(f"{key}_TIMEOUT") or "120")
        max_tokens = int(os.getenv(f"{key}_MAX_TOKENS") or "4096")
        temperature = float(os.getenv(f"{key}_TEMPERATURE") or "0.7")
        fallback_models = [
            item.strip()
            for item in _env_str(f"{key}_FALLBACK_MODELS", "").split(",")
            if item.strip()
        ]

        if provider == "openrouter":
            base_url = _env_str(f"{key}_BASE_URL", "https://openrouter.ai/api/v1")
        elif provider == "omniroute":
            base_url = _env_str(f"{key}_BASE_URL", "http://localhost:20129/v1")
        elif provider == "local":
            base_url = _env_str(f"{key}_BASE_URL", "http://localhost:11434/v1")

        model = _qualify_model(model, provider)
        fallback_models = [_qualify_model(item, provider) for item in fallback_models]

        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
            provider=provider,
            fallback_models=fallback_models,
        )


@dataclass
class ChatResult:
    messages: list[dict[str, Any]]
    content: str
    tool_calls: list[dict[str, Any]]


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _tool_call_dict(call: Any) -> dict[str, Any]:
    if isinstance(call, dict):
        return call
    function = _get_attr(call, "function", None)
    return {
        "id": _get_attr(call, "id", ""),
        "type": _get_attr(call, "type", "function"),
        "function": {
            "name": _get_attr(function, "name", "") if function else "",
            "arguments": _get_attr(function, "arguments", "{}") if function else "{}",
        },
    }


def _message_dict(message: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "role": "assistant",
        "content": _get_attr(message, "content", None),
    }
    tool_calls = _get_attr(message, "tool_calls", None)
    if tool_calls:
        result["tool_calls"] = [_tool_call_dict(call) for call in tool_calls]
    return result


class LLMClient:
    """Async LLM client backed by LiteLLM with model fallback and cost tracking."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig()
        self.last_metrics: dict[str, Any] = {}

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        response, _ = await self._acompletion(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        choice = _get_attr(response, "choices", [None])[0] if _get_attr(response, "choices", None) else None
        message = _get_attr(choice, "message", None)
        return _get_attr(message, "content", "") or ""

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        response, _ = await self._acompletion(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        choice = _get_attr(response, "choices", [None])[0] if _get_attr(response, "choices", None) else None
        message = _get_attr(choice, "message", None)
        assistant_message = _message_dict(message)
        updated_messages = list(messages)
        updated_messages.append(assistant_message)
        tool_calls = assistant_message.get("tool_calls") or []
        content = "" if tool_calls else (assistant_message.get("content") or "")
        return ChatResult(
            messages=updated_messages,
            content=content,
            tool_calls=tool_calls,
        )

    async def _acompletion(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> tuple[Any, str]:
        primary = model or self.config.model
        candidates = [primary, *self.config.fallback_models]
        last_exc: Exception | None = None
        start = time.monotonic()
        for candidate in candidates:
            try:
                kwargs = self._build_kwargs(
                    candidate,
                    messages,
                    tools,
                    max_tokens,
                    temperature,
                )
                litellm_module = _get_litellm()
                response = await litellm_module.acompletion(**kwargs)
                fallback_used = candidate if candidate != primary else ""
                self._record_metrics(response, candidate, fallback_used, start)
                return response, candidate
            except Exception as exc:
                last_exc = exc
                logger.warning("litellm request failed model=%s: %s", candidate, exc)
        if last_exc is None:
            raise RuntimeError("no LLM models configured")
        raise last_exc

    def _build_kwargs(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        max_tokens: int | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "stream": False,
            "timeout": self.config.timeout,
        }
        custom_provider = self._custom_provider_for(model)
        if custom_provider:
            kwargs["custom_llm_provider"] = custom_provider
        if self.config.api_key and self.config.api_key.strip():
            kwargs["api_key"] = self.config.api_key.strip()
        if self.config.base_url:
            kwargs["api_base"] = self.config.base_url
        if self.config.extra_headers:
            kwargs["extra_headers"] = dict(self.config.extra_headers)
        if tools is not None:
            kwargs["tools"] = tools
        return kwargs

    def _custom_provider_for(self, model: str) -> str:
        """Pick a LiteLLM provider for bare model names."""
        provider = (self.config.provider or "direct").lower()
        if provider in _LITELLM_PROVIDERS:
            return _LITELLM_PROVIDERS[provider]
        if provider in _OPENAI_COMPATIBLE_PROVIDERS:
            return "openai"
        if provider in ("local", "ollama"):
            return "openai"
        base_url = (self.config.base_url or "").lower()
        # NVIDIA NIM exposes an OpenAI-compatible API, but LiteLLM does not
        # recognize the model's "nvidia/" prefix as a provider.
        if "nvidia" in base_url:
            return "openai"
        if "/" in model:
            return ""
        if "deepseek" in base_url:
            return "deepseek"
        if "anthropic" in base_url:
            return "anthropic"
        if "openrouter" in base_url:
            return "openrouter"
        if any(host in base_url for host in ("localhost", "127.0.0.1", "omniroute")):
            return "openai"
        if base_url and "api.openai.com" not in base_url:
            return "openai"
        return "openai"

    def _record_metrics(
        self,
        response: Any,
        model: str,
        fallback_used: str,
        start: float,
    ) -> None:
        usage = _get_attr(response, "usage", None)
        prompt_tokens = int(_get_attr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(_get_attr(usage, "completion_tokens", 0) or 0)
        cost_usd = 0.0
        litellm_module = _get_litellm()
        try:
            cost_usd = float(litellm_module.completion_cost(completion_response=response))
        except Exception:
            try:
                cost_usd = float(
                    litellm_module.completion_cost(
                        model=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                )
            except Exception:
                cost_usd = 0.0
        self.last_metrics = {
            "model_used": model,
            "cost_usd": round(cost_usd, 6),
            "llm_latency_ms": round((time.monotonic() - start) * 1000, 1),
            "fallback_used": fallback_used,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
