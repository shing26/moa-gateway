from __future__ import annotations

import asyncio

import pytest

from app.router.intent_router import IntentRouter


class FakeClassifier:
    def __init__(self, intent: str = "assistant", delay: float = 0.0) -> None:
        self.intent = intent
        self.delay = delay
        self.calls = 0

    async def classify(self, text: str) -> str:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.intent


@pytest.mark.asyncio
async def test_micro_llm_used_when_regex_misses() -> None:
    micro = FakeClassifier(intent="translate")
    router = IntentRouter(micro_llm=micro)
    intent, fallback = await router.route("今天天气怎么样")
    assert intent == "translate"
    assert fallback == "micro_llm"
    assert micro.calls == 1


@pytest.mark.asyncio
async def test_router_llm_used_when_micro_unavailable() -> None:
    router_llm = FakeClassifier(intent="analyze")
    router = IntentRouter(router_llm=router_llm)
    intent, fallback = await router.route("今天天气怎么样")
    assert intent == "analyze"
    assert fallback == "router_llm"


@pytest.mark.asyncio
async def test_micro_timeout_falls_back_to_router_llm() -> None:
    micro = FakeClassifier(delay=0.05)
    router_llm = FakeClassifier(intent="search")
    router = IntentRouter(
        micro_llm=micro,
        router_llm=router_llm,
        micro_timeout_ms=5,
    )
    intent, fallback = await router.route("今天天气怎么样")
    assert intent == "search"
    assert fallback == "router_llm"
    assert router_llm.calls == 1
