from __future__ import annotations

import pytest

from app.agents.contract import AgentEnvelope
from app.agents.review_agent import ReviewAgent
from app.config import settings
from apps.code_review_pipeline.schemas.pipeline import AgentFindingResult, Finding, PipelineResult
from apps.code_review_pipeline.schemas.pr_context import PRContext, PRFile


def _pipeline_result(repo: str = "shing26/moa-gateway", pr_number: int = 1):
    pr = PRContext(
        repo=repo,
        pr_number=pr_number,
        head_sha="abc",
        base_sha="def",
        title="test",
        author="shing26",
        html_url=f"https://github.com/{repo}/pull/{pr_number}",
        diff_url=f"https://github.com/{repo}/pull/{pr_number}.diff",
        changed_files=[PRFile(filename="a.py", status="modified", additions=1, deletions=0, changes=1, patch="+x")],
    )
    finding = Finding(
        id="1",
        severity="critical",
        category="security",
        file="a.py",
        line=1,
        title="bad",
        description="desc",
        suggestion="fix",
    )
    report = AgentFindingResult(
        agent="report",
        trace_id="t1",
        findings=(finding,),
        summary="found issue",
        recommendation="request_changes",
        need_human_review=True,
    )
    result = PipelineResult(
        trace_id="t1",
        pr=pr,
        triage=report,
        static_analysis=report,
        semantic_review=report,
        test_coverage=report,
        report=report,
        overall_need_human_review=True,
    )
    return pr, result


class DummyPipeline:
    async def run(self, event):
        body = event.context or {}
        repo = body.get("repository", {}).get("full_name", "shing26/moa-gateway")
        pr_number = int(body.get("pull_request", {}).get("number", body.get("number", 1)))
        return _pipeline_result(repo=repo, pr_number=pr_number)


def _envelope(user_input: str, slot: dict | None = None) -> AgentEnvelope:
    return AgentEnvelope(
        trace_id="trace-1",
        session_id="session-1",
        user_raw_input=user_input,
        global_summary="",
        agent_local_slot=slot or {},
    )


@pytest.mark.asyncio
async def test_review_agent_formats_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "test-token")
    monkeypatch.setattr(ReviewAgent, "_build_pipeline", lambda self: DummyPipeline())

    output = await ReviewAgent().execute(_envelope("shing26/moa-gateway#1"))

    assert "PR: shing26/moa-gateway#1" in output
    assert "request_changes" in output
    assert "发现: 5 条" in output
    assert "需要人工复核: 是" in output


@pytest.mark.asyncio
async def test_review_agent_uses_webhook_body_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "test-token")
    monkeypatch.setattr(ReviewAgent, "_build_pipeline", lambda self: DummyPipeline())
    body = {
        "action": "opened",
        "number": 2,
        "pull_request": {"number": 2, "title": "webhook"},
        "repository": {"full_name": "owner/repo"},
    }

    output = await ReviewAgent().execute(_envelope("", {"webhook_body": body}))

    assert "PR: owner/repo#2" in output


@pytest.mark.asyncio
async def test_review_agent_graceful_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "")

    output = await ReviewAgent().execute(_envelope("shing26/moa-gateway#1"))

    assert "GITHUB_TOKEN" in output


@pytest.mark.asyncio
async def test_review_agent_asks_for_pr_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "test-token")

    output = await ReviewAgent().execute(_envelope("随便聊聊"))

    assert "owner/repo#PR号" in output
