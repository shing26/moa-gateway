from __future__ import annotations

import pytest

from app.router.llm_classifier import LLMIntentClassifier


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = []

    async def chat(self, messages: list[dict], **kwargs) -> str:
        self.calls.append((messages, kwargs))
        return self.reply


@pytest.mark.asyncio
async def test_classify_returns_valid_intent() -> None:
    llm = FakeLLM(" coding\n")
    classifier = LLMIntentClassifier(llm)
    assert await classifier.classify("帮我写代码") == "coding"
    assert llm.calls[0][1]["max_tokens"] == 32
    assert llm.calls[0][1]["temperature"] == 0.0


@pytest.mark.asyncio
async def test_classify_passes_timeout_into_llm_call() -> None:
    """超时必须"传进去"而不是路由层外部取消（litellm 被外部取消会泄漏协程，ADR-016）。"""
    llm = FakeLLM(" coding")
    classifier = LLMIntentClassifier(llm)
    await classifier.classify("帮我写代码", timeout_s=2.5)
    assert llm.calls[0][1]["timeout"] == 2.5


@pytest.mark.asyncio
async def test_classify_falls_back_to_assistant_on_noise() -> None:
    llm = FakeLLM("我不确定，可能是 general")
    classifier = LLMIntentClassifier(llm)
    assert await classifier.classify("随便聊聊") == "assistant"
