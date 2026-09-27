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

    async def classify(self, text: str, *, timeout_s: float | None = None) -> str:
        """把一段输入路由到意图标签。

        ``timeout_s`` 传给 LLM 客户端自身（litellm 的 timeout），而**不是**由路由层
        从外部取消——``asyncio.wait_for`` 的外部取消会让 litellm 留下未 await 的内部
        协程（``RuntimeWarning``，2026-09-24 定位为上游缺陷且 1.102.1 仍未修），且取消
        会让本地小模型一直热不起来。见 ADR-016/第十二轮。
        """
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
                timeout=timeout_s,
            )
        ).strip().lower()
        for token in re.split(r"[\s,，。.!！]+", reply):
            if token in VALID_INTENTS:
                return token
        return "assistant"


__all__ = ["LLMIntentClassifier"]
