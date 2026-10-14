"""回归测试：PR 审查流水线的 need_human_review 必须真的能变成 True。

这个文件钉住一个已经修掉的缺陷。缺陷链条：

1. ``ReportAgent.execute`` 返回的是 ``AgentFindingResult`` 对象，内部
   ``_parse_report`` 已用 ``_infer_hitl`` 按 severity 推断出 need_human_review。
2. 但 ``CodeReviewPipeline.run`` 把该对象交给 ``_agent_result(agent, trace_id, raw)``，
   而当时的 ``_agent_result`` 只接受 ``str``，于是 ``json.loads(<对象>)`` 抛错、
   落进 ``{"raw": obj}`` 分支：findings 全丢、summary 变空、need_human_review=False。
3. 于是第113-119 行的 OR 聚合恒为 False，经 ``app/worker.py`` 落库后飞书卡片
   也永不带人工复核标记——五个agent 报出 critical 也不刹车。

为什么此前1078 个单测没抓到：``test_guard_hitl_loop.py`` 的 DummyPipeline
**手工构造** PipelineResult 并直接写 overall_need_human_review=True，
根本没有执行真实的 _agent_result。所以本文件必须真跑 run()。
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.models.events import MoAEvent
from apps.code_review_pipeline.agents.code_review_pipeline import (
    CodeReviewPipeline,
    _agent_result,
)
from apps.code_review_pipeline.schemas.pipeline import AgentFindingResult, Finding
from apps.code_review_pipeline.schemas.pr_context import PRContext, PRFile


def _pr_context() -> PRContext:
    return PRContext(
        repo="shing26/moa-gateway",
        pr_number=7,
        head_sha="head-sha",
        base_sha="base-sha",
        title="fix: something dangerous",
        author="shing26",
        html_url="https://github.com/shing26/moa-gateway/pull/7",
        diff_url="https://github.com/shing26/moa-gateway/pull/7.diff",
        changed_files=[
            PRFile(
                filename="app/pipeline.py",
                status="modified",
                additions=1,
                deletions=0,
                changes=1,
                patch="+x = 1",
                sha="file-sha",
                content="x = 1",
            )
        ],
    )


def _event() -> MoAEvent:
    from app.fsm.state_machine import Event

    return MoAEvent(
        trace_id="trace-critical",
        event=Event.MESSAGE_RECEIVED,
        session_id="session-1",
        text="review this PR",
        context={},
    )


class _StubLLM:
    """按调用顺序回放预设回复的假 LLM。"""

    def __init__(self, replies: dict[str, str]) -> None:
        self._replies = replies
        self.calls: list[str] = []

    async def chat(self, messages: list[dict[str, str]]) -> str:
        system = messages[0]["content"]
        for marker, reply in self._replies.items():
            if marker in system:
                self.calls.append(marker)
                return reply
        return "{}"


def _pipeline_for(replies: dict[str, str]) -> CodeReviewPipeline:
    """构造一个 run() 内部会用到的所有 agent 都返回固定文本的流水线。"""
    llm = _StubLLM(replies)
    pipeline = CodeReviewPipeline(github_client=AsyncMock())
    # 五个 agent 各自 new 自己的 LLM，统一替换为同一个 stub
    for name in ("_triage", "_static", "_semantic", "_test", "_report"):
        agent = getattr(pipeline, name)
        agent._llm = llm
    return pipeline


_CLEAN_REPLY = json.dumps(
    {"findings": [], "summary": "ok", "recommendation": "approve"}
)

# ReportAgent 报出一个 critical finding；按 _infer_hitl 规则必须需要人工复核。
_CRITICAL_REPLY = json.dumps(
    {
        "findings": [
            {
                "id": "final-001",
                "severity": "critical",
                "category": "security",
                "file": "app/pipeline.py",
                "line": 42,
                "title": "hardcoded secret",
                "description": "token embedded in source",
                "suggestion": "load from env",
                "confidence": 0.95,
            }
        ],
        "summary": "critical issue found",
        "recommendation": "request_changes",
    }
)


class TestAgentResultNormalisation:
    """_agent_result 的两种输入形态。"""

    def test_structured_result_is_passed_through(self) -> None:
        """已算好的 AgentFindingResult 必须原样保留，不能再解析一遍。"""
        finding = Finding(
            id="f1",
            severity="critical",
            category="security",
            file="a.py",
            line=1,
            title="t",
            description="d",
            suggestion="s",
        )
        prepared = AgentFindingResult(
            agent="report",
            trace_id="t1",
            findings=(finding,),
            summary="bad",
            recommendation="request_changes",
            need_human_review=True,
        )

        out = _agent_result("report", "t1", prepared)

        assert out.need_human_review is True
        assert len(out.findings) == 1
        assert out.summary == "bad"

    def test_json_text_is_parsed(self) -> None:
        raw = json.dumps({"findings": [], "summary": "s", "recommendation": "comment"})
        out = _agent_result("static_analysis", "t1", raw)
        assert out.agent == "static_analysis"
        assert out.summary == "s"

    def test_json_text_can_request_review_explicitly(self) -> None:
        raw = json.dumps({"findings": [], "need_human_review": True})
        assert _agent_result("semantic_review", "t1", raw).need_human_review is True

    def test_clean_json_text_defaults_to_no_review(self) -> None:
        raw = json.dumps({"findings": [], "recommendation": "approve"})
        assert _agent_result("semantic_review", "t1", raw).need_human_review is False

    def test_unparsable_payload_escalates(self) -> None:
        """agent 返回垃圾文本不能被当成"零发现 + 无需复核"。"""
        out = _agent_result("triage", "t1", "not json at all")
        assert out.need_human_review is True
        assert out.summary == "parse failed"


class TestPipelineReviewEscalation:
    """端到端：真跑 run()，验证 critical 能一路冒到 overall_need_human_review。"""

    def test_critical_finding_sets_overall_flag(self) -> None:
        pipeline = _pipeline_for(
            {
                "分类助手": _CLEAN_REPLY,
                "静态": _CLEAN_REPLY,
                "语义": _CLEAN_REPLY,
                "覆盖": _CLEAN_REPLY,
                "报告生成": _CRITICAL_REPLY,
            }
        )

        with patch(
            "apps.code_review_pipeline.agents.code_review_pipeline.build_pr_context_from_github",
            AsyncMock(return_value=_pr_context()),
        ), patch(
            "apps.code_review_pipeline.agents.code_review_pipeline.retrieve_team_patterns",
            AsyncMock(return_value=None),
        ):
            _, result = asyncio_run(pipeline.run(_event()))

        assert result.report.need_human_review is True
        assert result.report.recommendation == "request_changes"
        assert result.overall_need_human_review is True

    def test_clean_review_stays_false(self) -> None:
        """反向对照：干净 PR 不应产生误报。"""
        pipeline = _pipeline_for(
            {
                "分类助手": _CLEAN_REPLY,
                "静态": _CLEAN_REPLY,
                "语义": _CLEAN_REPLY,
                "覆盖": _CLEAN_REPLY,
                "报告生成": _CLEAN_REPLY,
            }
        )

        with patch(
            "apps.code_review_pipeline.agents.code_review_pipeline.build_pr_context_from_github",
            AsyncMock(return_value=_pr_context()),
        ), patch(
            "apps.code_review_pipeline.agents.code_review_pipeline.retrieve_team_patterns",
            AsyncMock(return_value=None),
        ):
            _, result = asyncio_run(pipeline.run(_event()))

        assert result.overall_need_human_review is False


def asyncio_run(coro: Any) -> Any:
    """在测试里同步驱动一个协程（pytest-asyncio 未全量启用时的最小依赖方案）。"""
    import asyncio

    return asyncio.run(coro)
