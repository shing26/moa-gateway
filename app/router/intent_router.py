from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from app.fsm.state_machine import Event, State


class RouterLLM(Protocol):
    async def classify(self, text: str) -> str: ...


class IntentRouter:
    def __init__(
        self,
        router_llm: RouterLLM | None = None,
        micro_llm: RouterLLM | None = None,
        router_timeout_ms: int = 2000,
        micro_timeout_ms: int = 1000,
    ) -> None:
        self.router_llm = router_llm
        self.micro_llm = micro_llm
        self.router_timeout_ms = router_timeout_ms
        self.micro_timeout_ms = micro_timeout_ms
        self._regex_map = [
            (re.compile(r"^(hi|hello|hey|你好|您好)$", re.IGNORECASE), "greeting"),
            (re.compile(r"(debug|错误|报错|traceback|exception)", re.IGNORECASE), "debug"),
            (re.compile(r"(code|代码|函数|class|模块|实现|refactor)", re.IGNORECASE), "coding"),
            (re.compile(r"(cancel|取消|重置|reset|stop)", re.IGNORECASE), "control"),
            (re.compile(r"(翻译|translate|英文|中文|english)", re.IGNORECASE), "translate"),
            (re.compile(r"(总结|摘要|summarize|概括|提炼)", re.IGNORECASE), "summarize"),
            (re.compile(r"(搜索|search|查找|查询|find)", re.IGNORECASE), "search"),
            (re.compile(r"(分析|analyze|统计|compare|对比|比较)", re.IGNORECASE), "analyze"),
        ]
        self.default_intent = "assistant"

    async def route(self, text: str) -> tuple[str, str]:
        intent, fallback_level = await self._regex_fallback(text)
        if intent != self.default_intent:
            return intent, fallback_level

        intent, fallback_level = await self._micro_llm_fallback(text)
        if intent != self.default_intent:
            return intent, fallback_level

        intent, fallback_level = await self._router_llm_fallback(text)
        return intent, fallback_level

    async def _regex_fallback(self, text: str) -> tuple[str, str]:
        for pattern, intent in self._regex_map:
            if pattern.search(text):
                return intent, "regex"
        return self.default_intent, "none"

    async def _micro_llm_fallback(self, text: str) -> tuple[str, str]:
        if self.micro_llm is None:
            return self.default_intent, "none"
        # Placeholder timeout guard omitted for scaffolding.
        try:
            intent = await self.micro_llm.classify(text)
            if intent and intent != self.default_intent:
                return intent, "micro_llm"
        except Exception:
            pass
        return self.default_intent, "none"

    async def _router_llm_fallback(self, text: str) -> tuple[str, str]:
        if self.router_llm is None:
            return self.default_intent, "none"
        try:
            intent = await self.router_llm.classify(text)
            return intent or self.default_intent, "router_llm"
        except Exception:
            return self.default_intent, "none"
