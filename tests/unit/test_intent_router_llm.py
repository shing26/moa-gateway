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


@pytest.mark.asyncio
async def test_specific_tool_intents_win_over_broad_task_phrase() -> None:
    router = IntentRouter()

    search_intent, _ = await router.route("帮我搜索项目文档")
    task_intent, _ = await router.route("帮我算 3*7")

    assert search_intent == "search"
    assert task_intent == "task"


@pytest.mark.asyncio
async def test_bare_suan_is_not_a_task_trigger() -> None:
    """`算` 不能裸写进 task 正则：它是「预**算**」「打**算**」「核**算**」的一部分。

    回归（2026-09-23）：实测「预算表里的数字对不上」与「这活儿我算下来要三天」
    都被裸 `算` 吞成 task —— 后者会把一句普通陈述送进 TaskAgent。改为只保留明确
    的动词短语后两者不再命中，而真计算请求仍然命中。
    """
    router = IntentRouter()

    for text in ("预算表里的数字对不上", "这活儿我算下来要三天"):
        intent, level = await router._regex_fallback(text)
        assert level == "none", f"{text} 不该被正则命中（裸 `算` 又回来了？）"

    for text in ("算一下 37*89", "算一算这个比例", "记一下明天开会"):
        intent, level = await router._regex_fallback(text)
        assert (intent, level) == ("task", "regex"), text


@pytest.mark.asyncio
async def test_write_style_coding_requests_are_caught() -> None:
    """「写一个 X 示例」这类**最典型的编码请求**此前完全不命中 coding。

    它会落到模型兜底甚至默认意图 `assistant` —— 与标签无关，这就是漏判。
    补上 `写一个|写个|编写` 后（实测，正则层确定性）：e2e 标签命中 23/30 → 29/30，
    而 intent 数据集准确率保持 1.00、一致性数据集的"必须不命中正则"约束仍 0 违反。

    选这三个而非裸 `写`：裸 `写`会把「写一份周报」也吞成 coding —— 下面留着反例。
    """
    router = IntentRouter()

    for text in ("写一个单例模式示例", "写一个爬虫示例", "写个二分查找"):
        intent, level = await router._regex_fallback(text)
        assert (intent, level) == ("coding", "regex"), text

    intent, level = await router._regex_fallback("写一份周报模板")
    assert level == "none", "非编码的「写」不该被吞（守住裸 `写` 的误吞）"
