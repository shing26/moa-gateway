from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("moa.agents.provider")


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout: float = 120.0
    max_tokens: int = 4096
    temperature: float = 0.7
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ChatResult:
    messages: list[dict[str, Any]]
    content: str
    tool_calls: list[dict[str, Any]]


class LLMClient:
    """Async HTTP client for OpenAI-compatible chat completion APIs."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.api_key and self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"
        headers.update(self.config.extra_headers)
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            headers=headers,
            timeout=self.config.timeout,
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": messages,
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,"stream": False,
        }
        logger.debug("llm request: model=%s messages=%d", payload["model"], len(messages))
        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        content: str = choice["message"]["content"] or ""
        logger.debug("llm response: finish=%s tokens=%d", choice.get("finish_reason"), data.get("usage", {}))
        return content

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "stream": False,
        }
        logger.debug(
            "llm tools request: model=%s messages=%d tools=%d",
            payload["model"],
            len(messages),
            len(tools),
        )
        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        updated_messages = list(messages)
        updated_messages.append(message)
        tool_calls: list[dict[str, Any]] = message.get("tool_calls") or []
        content = "" if tool_calls else (message.get("content") or "")
        logger.debug(
            "llm tools response: finish=%s tool_calls=%d",
            choice.get("finish_reason"),
            len(tool_calls),
        )
        return ChatResult(messages=updated_messages, content=content, tool_calls=tool_calls)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
