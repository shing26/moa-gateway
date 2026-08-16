from __future__ import annotations

import re
from typing import Any

VALID_INTENTS = {
    "coding",
    "translate",
    "summarize",
    "search",
    "analyze",
    "greeting",
    "debug",
    "control",
    "assistant",
}


class LLMIntentClassifier:
    """Routes a message to an intent label using an LLM chat client."""

    def __init__(self, llm: Any, max_tokens: int = 32, temperature: float = 0.0) -> None:
        self._llm = llm
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def classify(self, text: str) -> str:
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是意图路由器。只输出以下意图标签之一："
                    "coding, translate, summarize, search, analyze, greeting, debug, control, assistant。"
                ),
            },
            {"role": "user", "content": f"消息：{text[:500]}\n意图标签："},
        ]
        reply = (
            await self._llm.chat(
                prompt,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        ).strip().lower()
        for token in re.split(r"[\s,，。.!！]+", reply):
            if token in VALID_INTENTS:
                return token
        return "assistant"


__all__ = ["LLMIntentClassifier"]
