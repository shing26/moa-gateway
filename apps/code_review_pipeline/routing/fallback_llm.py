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

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        try:
            return await self._primary.chat(messages, **kwargs)
        except Exception as exc:
            logger.warning("primary llm failed: %s", exc)
            if self._fallback is None:
                raise
            logger.info("falling back to secondary provider")
            return await self._fallback.chat(messages, **kwargs)

    async def chat_with_tools(self, messages: list[dict], tools: list[dict], **kwargs: Any) -> Any:
        try:
            return await self._primary.chat_with_tools(messages, tools, **kwargs)
        except Exception as exc:
            logger.warning("primary llm tools failed: %s", exc)
            if self._fallback is None:
                raise
            logger.info("falling back to secondary provider (tools)")
            return await self._fallback.chat_with_tools(messages, tools, **kwargs)

    async def aclose(self) -> None:
        await self._primary.aclose()
        if self._fallback is not None:
            await self._fallback.aclose()
