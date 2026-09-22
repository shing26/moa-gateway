"""意图一致性维度：同输入重放 N 次，测判定是否收敛。

这个维度的价值全在"没有期望值"——只看收敛与否，所以设计者无法自证。
相应地，它的守卫也不是"数值达标"，而是**测量本身是否成立**：
数据集用例必须绕开正则表（否则测的是确定性代码，必然 1.0）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.router.intent_router import IntentRouter
from evals.run_evals import (
    _consistency_skipped,
    load_dataset,
    run_intent_consistency_eval,
)

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evals" / "datasets" / "intent_consistency.jsonl"


class ScriptedRouter:
    """按调用次序循环吐出预设 intent，用来造出"摆动"与"稳定"两种情形。"""

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)
        self.calls = 0

    async def route(self, text: str) -> tuple[str, str]:
        intent = self._script[self.calls % len(self._script)]
        self.calls += 1
        return intent, "scripted"


@pytest.mark.asyncio
async def test_dataset_cases_bypass_the_regex_table() -> None:
    """每条用例都必须**不命中**任何意图正则。

    这是本维度的漂移守卫：一旦有人往数据集里加了一条会被正则直接命中的输入，
    这条断言就红——因为那种输入是确定性的，重放 N 次必然一致，测出来的 1.0 是
    正则表的功劳，不是模型路径的稳定性。
    """
    cases = load_dataset(DATASET)
    assert cases, "数据集不能为空"

    router = IntentRouter()
    for case in cases:
        _intent, level = await router._regex_fallback(str(case.get("input", "")))
        assert level == "none", (
            f"{case.get('id')} 命中正则（{level}）——该用例无法测模型路径稳定性："
            f"{case.get('input')}"
        )


@pytest.mark.asyncio
async def test_flapping_router_is_detected() -> None:
    """两个意图交替 → 稳定率 0，一致度等于多数派占比，并逐条列出不稳定用例。"""
    cases = [{"id": "c1", "input": "客户问能不能下周交付"}]
    router = ScriptedRouter(["assistant", "translate"])

    result = await run_intent_consistency_eval(cases, repeats=5, router=router)

    assert result["repeats"] == 5
    assert result["stable"] == 0
    assert result["stable_rate"] == 0.0
    # 5 次里多数派出现 3 次
    assert result["avg_agreement"] == 0.6
    assert len(result["unstable"]) == 1
    assert result["unstable"][0]["intents"] == {"assistant": 3, "translate": 2}
    assert result["levels"] == {"scripted": 5}
    assert result["degraded_calls"] == 0
    assert result["note"] == ""


@pytest.mark.asyncio
async def test_deterministic_router_is_stable() -> None:
    cases = [{"id": "c1", "input": "客户问能不能下周交付"}]
    router = ScriptedRouter(["assistant"])

    result = await run_intent_consistency_eval(cases, repeats=4, router=router)

    assert result["stable"] == 1
    assert result["stable_rate"] == 1.0
    assert result["avg_agreement"] == 1.0
    assert result["unstable"] == []


@pytest.mark.asyncio
async def test_stable_rate_mixes_stable_and_flapping_inputs() -> None:
    """一半输入稳定、一半摆动 → 0.5：稳定率是按输入算的，不是按调用算的。"""
    cases = [
        {"id": "stable", "input": "这句稳定"},
        {"id": "flappy", "input": "这句摆动"},
    ]

    class CountingRouter:
        def __init__(self) -> None:
            self.n = 0

        async def route(self, text: str) -> tuple[str, str]:
            if "摆动" in text:
                self.n += 1
                return ("assistant" if self.n % 2 else "translate"), "scripted"
            return "assistant", "scripted"

    result = await run_intent_consistency_eval(cases, repeats=5, router=CountingRouter())

    assert result["stable"] == 1
    assert result["stable_rate"] == 0.5
    assert [u["id"] for u in result["unstable"]] == ["flappy"]


def test_offline_shape_is_skipped_not_faked() -> None:
    """离线必须标 skipped，而不是报一个 1.0——纯正则是确定性的，那个 1.0 是假的。"""
    result = _consistency_skipped([{"id": "c1", "input": "x"}], repeats=5)

    assert result["skipped"] == 1
    assert result["stable_rate"] == 0.0
    assert result["stable"] == 0
    assert result["total"] == 1


class DegradedRouter:
    """永远降级到默认意图 —— 模拟"路由 LLM 没给出判定"。"""

    async def route(self, text: str) -> tuple[str, str]:
        return "assistant", "none"


@pytest.mark.asyncio
async def test_all_degraded_is_flagged_not_taken_as_stable_judgement() -> None:
    """全部降级时稳定率 1.0 是"一致地降级"，报告必须自己说清楚。

    这正是本维度第一次跑就撞上的真实情形：冷启动首次调用 4.2s 超过
    ROUTER_LLM_TIMEOUT_MS=2000，超时取消后模型热不起来，55 次调用全部降级成
    默认意图——而只看 stable_rate 会以为路由很稳。
    """
    cases = [{"id": "c1", "input": "客户问能不能下周交付"}]

    result = await run_intent_consistency_eval(cases, repeats=5, router=DegradedRouter())

    assert result["stable_rate"] == 1.0        # 数字本身确实是 1.0
    assert result["levels"] == {"none": 5}     # 但层级说明它是降级
    assert result["degraded_calls"] == 5
    assert "全部调用降级" in result["note"]


@pytest.mark.asyncio
async def test_partial_degradation_is_noted() -> None:
    """部分降级同样要写进 note：稳定率被高估，只是没到全降级那么严重。"""
    cases = [{"id": "c1", "input": "客户问能不能下周交付"}]

    class HalfDegraded:
        def __init__(self) -> None:
            self.n = 0

        async def route(self, text: str) -> tuple[str, str]:
            self.n += 1
            return ("assistant", "none") if self.n % 2 else ("translate", "router_llm")

    result = await run_intent_consistency_eval(cases, repeats=4, router=HalfDegraded())

    assert result["levels"] == {"none": 2, "router_llm": 2}
    assert result["degraded_calls"] == 2
    assert "2/4" in result["note"]
