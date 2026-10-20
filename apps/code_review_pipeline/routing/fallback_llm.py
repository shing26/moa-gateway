from __future__ import annotations

import logging
from typing import Any

from app.agents.provider import LLMClient, LLMConfig

logger = logging.getLogger("moa.code_review.llm")


class FallbackLLMClient:
    """Primary -> fallback provider wrapper with automatic failover."""

    def __init__(self, primary: LLMClient, fallback: LLMClient | None = None) -> None:
        self._primary = primary
        self._fallback = fallback
        self._last_client: LLMClient = primary

    @property
    def last_metrics(self) -> dict[str, Any]:
        return dict(self._last_client.last_metrics)

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        try:
            result = await self._primary.chat(messages, **kwargs)
            self._last_client = self._primary
            return result
        except Exception as exc:
            logger.warning("primary llm failed: %s", exc)
            if self._fallback is None:
                raise
            logger.info("falling back to secondary provider")
            result = await self._fallback.chat(messages, **kwargs)
            self._last_client = self._fallback
            return result

    async def chat_with_tools(self, messages: list[dict], tools: list[dict], **kwargs: Any) -> Any:
        try:
            result = await self._primary.chat_with_tools(messages, tools, **kwargs)
            self._last_client = self._primary
            return result
        except Exception as exc:
            logger.warning("primary llm tools failed: %s", exc)
            if self._fallback is None:
                raise
            logger.info("falling back to secondary provider (tools)")
            result = await self._fallback.chat_with_tools(messages, tools, **kwargs)
            self._last_client = self._fallback
            return result

    async def aclose(self) -> None:
        await self._primary.aclose()
        if self._fallback is not None:
            await self._fallback.aclose()
