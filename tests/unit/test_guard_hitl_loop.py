"""Guardrail + HITL closed-loop tests for Code Review Pipeline."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.agents.contract import AgentEnvelope
from app.guard.guard_service import guard_service
from app.guard.rbac import GuardianAction
from apps.code_review_pipeline.agents.code_review_pipeline import CodeReviewPipeline
from apps.code_review_pipeline.schemas.pipeline import AgentFindingResult, Finding, PipelineResult
from apps.code_review_pipeline.schemas.pr_context import PRContext, PRFile
from app.main import app
from apps.code_review_pipeline.routing.github_client import GitHubClient


class DummyLLM:
    async def chat(self, messages: list[dict[str, str]]) -> str:
        return "{}"


class DummyPipeline(CodeReviewPipeline):
    def __init__(self) -> None:
        pass

    async def run(self, event: Any) -> tuple[PRContext, PipelineResult]:
        pr = PRContext(
            repo="shing26/moa-gateway",
            pr_number=1,
            head_sha="abc",
            base_sha="def",
            title="test",
            author="shing26",
            html_url="https://github.com/shing26/moa-gateway/pull/1",
            diff_url="https://github.com/shing26/moa-gateway/pull/1.diff",
            changed_files=(PRFile(filename="a.py", status="modified", additions=1, deletions=0, changes=1, patch="+x=1", sha="1", content="+x=1"),),
            labels=(),
            reviewers=(),
        )
        finding = Finding(id="1", severity="critical", category="security", file="a.py", line=1, title="t", description="d", suggestion="s")
        report = AgentFindingResult(agent="report", trace_id="t1", findings=(finding,), summary="bad", recommendation="request_changes", need_human_review=True)
        return pr, PipelineResult(trace_id="t1", pr=pr, triage=report, static_analysis=report, semantic_review=report, test_coverage=report, report=report, overall_need_human_review=True)


@pytest.fixture(autouse=True)
def _patch_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CodeReviewPipeline, "from_env", lambda: DummyPipeline())


def test_guard_deny_returns_blocked() -> None:
    verdict, policy_ids = guard_service.evaluate_output("服务器地址 10.0.0.1, 优惠价只要 99 元")
    assert verdict.action == GuardianAction.DENY
    assert "policy.security.internal_ip" in policy_ids


def test_hitl_review_for_compliance() -> None:
    verdict, policy_ids = guard_service.evaluate_output("优惠价只要 99 元")
    assert verdict.action == GuardianAction.REVIEW
    assert policy_ids == ("policy.compliance.no_price_commitment",)


def test_guard_allow_continues() -> None:
    verdict, _ = guard_service.evaluate_output("今天天气不错")
    assert verdict.action == GuardianAction.ALLOW
