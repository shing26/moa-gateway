"""上下文预算与压缩（上下文工程）：估算、裁剪、省略摘要 + pipeline 集成。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.pipeline as pipeline_module
from app.context_budget import compact_history, estimate_tokens, fit_text
from app.engine import Engine
from app.fsm.state_machine import Event as FsmEvent
from app.guard.rbac import GuardianAction, GuardVerdict
from app.models.events import MoAEvent, new_trace_id
from app.outbound.adapter import ResponseAdapter
from app.pipeline import MoAPipeline


# ── 估算 ────────────────────────────────────────────────────────────────────


def test_estimate_tokens_cjk_counts_per_char():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("中文十个字符测试用例啊") == 11


def test_estimate_tokens_ascii_and_mixed():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2
    # 混合：4 个 CJK + 8 个 ASCII（=2 token）
    assert estimate_tokens("你好世界abcdefgh") == 6


# ── 文本截断 ────────────────────────────────────────────────────────────────


def test_fit_text_disabled_and_within_budget():
    assert fit_text("内容", 0) == ("内容", False)  # 0 = 禁用
    text = "短内容"
    assert fit_text(text, 100) == (text, False)


def test_fit_text_truncates_and_marks():
    text = "甲" * 500
    fitted, truncated = fit_text(text, 100)
    assert truncated is True
    assert "已截断" in fitted
    assert estimate_tokens(fitted) <= 100 + 20  # 标记本身占一点余量


# ── 历史裁剪与省略摘要 ──────────────────────────────────────────────────────


def _history(pairs: int, chars: int = 100) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for i in range(pairs):
        out.append({"role": "user", "content": f"第{i}轮提问：" + "甲" * chars})
        out.append({"role": "assistant", "content": f"第{i}轮回答：" + "乙" * chars})
    return out


def test_compact_history_disabled_keeps_everything():
    hist = _history(5)
    res = compact_history(hist, 0)
    assert res.history == hist
    assert res.elision == ""
    assert res.dropped_messages == 0
    assert res.elided is False


def test_compact_history_keeps_newest_and_elides_old():
    hist = _history(20, chars=200)  # 约 8000 token
    res = compact_history(hist, 400)
    assert 0 < res.kept_messages < len(hist)
    assert res.dropped_messages == len(hist) - res.kept_messages
    # 保留的是最新的：最后一条仍在
    assert res.history[-1] == hist[-1]
    # 旧对话压成省略摘要而不是整段消失
    assert res.elided is True
    assert "已省略" in res.elision
    assert "user:" in res.elision


def test_compact_history_empty_and_tiny_budget():
    assert compact_history([], 100).history == []
    res = compact_history(_history(3, chars=200), 1)  # 预算小到装不下任何一轮
    assert res.history == []
    assert res.dropped_messages == 6


# ── pipeline 集成：长历史被裁剪且摘要并入上下文 ────────────────────────────


class FakeRetriever:
    async def retrieve(self, query, session_id=None, user_id=None):
        from app.vectordb.retriever import RetrievalResult

        return RetrievalResult(chunks=[], context="检索到的上下文", doc_count=1)


class FakeFlagClient:
    async def get(self, name, default=False):
        return False


class FakeEvaluator:
    async def score(self, output_text, intent):
        return SimpleNamespace(score=1.0, need_human_review=False)


class LongHistoryMemory:
    def __init__(self):
        self.records = _history(20, chars=200)

    def get_history(self, session_id):
        return list(self.records)

    def add(self, session_id, user_msg, assistant_msg):
        pass

    def clear(self, session_id):
        pass


class FakeRouter:
    async def route(self, text):
        return ("coding", "regex")


class FakeCommandMode:
    def set(self, session_id, mode):
        pass

    def get(self, session_id):
        return None

    def clear(self, session_id):
        pass


class FakeGuard:
    def evaluate(self, agent_name, intent, payload, *, hitl_enabled=True):
        return GuardVerdict(action=GuardianAction.ALLOW, reason="ok")


class RecordingAgent:
    def __init__(self):
        self.envelopes = []

    async def execute(self, envelope):
        self.envelopes.append(envelope)
        return "agent reply"


@pytest.mark.asyncio
async def test_pipeline_trims_history_and_merges_elision(monkeypatch):
    agent = RecordingAgent()
    monkeypatch.setattr(
        pipeline_module,
        "select_canary_version",
        lambda *a, **k: (SimpleNamespace(system_prompt="sys"), "stable"),
    )
    monkeypatch.setattr(pipeline_module, "get_agent", lambda name: agent)

    pipeline = MoAPipeline(
        engine=Engine(),
        router=FakeRouter(),
        memory=LongHistoryMemory(),
        adapter=ResponseAdapter(),
        evaluator=FakeEvaluator(),
        retriever=FakeRetriever(),
        prompt_registry=object(),
        flag_client=FakeFlagClient(),
        guard_service=FakeGuard(),
        command_mode=FakeCommandMode(),
    )
    event = MoAEvent(
        trace_id=new_trace_id(),
        event=FsmEvent.MESSAGE_RECEIVED,
        session_id="ctx-s1",
        text="继续",
        context={"source": "test"},
    )
    await pipeline.run(event, channel="test", target="ctx-s1")

    envelope = agent.envelopes[-1]
    # 40 条历史被裁剪到预算内，且最新一条保住
    assert len(envelope.history) < 40
    assert envelope.history[-1]["content"].startswith("第19轮回答")
    # 省略摘要并入 global_summary（检索上下文也还在）
    assert "已省略" in envelope.global_summary
    assert "检索到的上下文" in envelope.global_summary
