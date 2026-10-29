from __future__ import annotations

import pytest

from apps.code_review_pipeline.agents.code_review_pipeline import CodeReviewPipeline
from apps.code_review_pipeline.agents.review_critic import (
    ReviewCriticResult,
    ReviewCriticVerdict,
)
from apps.code_review_pipeline.schemas.pipeline import AgentFindingResult, Finding
from apps.code_review_pipeline.schemas.pr_context import PRContext
from apps.code_review_pipeline.storage.review_store import ReviewRecord, ReviewStore
from apps.code_review_pipeline.task_queue import TaskMessage


class _StaticAgent:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    async def execute(self, _envelope):
        return self.payload


class _ReportAgent:
    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.calls = 0

    async def execute(self, envelope):
        self.calls += 1
        if self.calls == 1:
            finding = Finding(
                id="candidate-1",
                severity="high",
                category="logic",
                file="app/service.py",
                line=10,
                title="Wrong retry boundary",
                description="The final retry is skipped.",
                suggestion="Use an inclusive retry boundary.",
                confidence=0.9,
                evidence=("diff:app/service.py:10",),
                source_agents=("semantic_review", "report"),
            )
            return AgentFindingResult(
                agent="report",
                trace_id=self.trace_id,
                findings=(finding,),
                summary="one candidate issue",
                recommendation="request_changes",
                need_human_review=True,
            )

        source_uid = envelope.agent_local_slot["candidate_findings"][0]["finding_uid"]
        revised = Finding(
            id="revised-1",
            severity="high",
            category="logic",
            file="app/service.py",
            line=10,
            title="Wrong retry boundary",
            description="The final retry is skipped.",
            suggestion="Use an inclusive retry boundary.",
            confidence=0.9,
            evidence=("diff:app/service.py:10",),
            source_agents=("semantic_review", "report"),
            revision_of=source_uid,
        )
        return AgentFindingResult(
            agent="report",
            trace_id=self.trace_id,
            findings=(revised,),
            summary="one revised issue",
            recommendation="request_changes",
            need_human_review=True,
        )


class _ReviewCritic:
    def __init__(self) -> None:
        self.calls = 0

    async def review(self, *, findings, **_kwargs):
        self.calls += 1
        return ReviewCriticResult(
            verdicts=(
                ReviewCriticVerdict(
                    finding_uid=findings[0].finding_uid,
                    decision="revise",
                    reason="The issue is valid but the wording needs to be narrower.",
                    instructions="Keep the issue and tighten the description.",
                ),
            ),
            metrics={"cost_usd": 0.01, "revise": 1},
        )


class _PipelineGitHub:
    async def get_pr_files(self, repo, pr_number):
        assert repo.owner == "owner"
        assert repo.name == "repo"
        assert pr_number == 12
        return [
            {
                "filename": "app/service.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "changes": 1,
                "patch": "@@ -9,3 +9,4 @@\n context\n+    return retry_count <= max_retries\n context",
            }
        ]


@pytest.mark.asyncio
async def test_worker_r1_feedback_publish_round_trip(monkeypatch) -> None:
    from app import worker as worker_module
    from app.routes import feishu

    trace_id = "cr_owner/repo#12@sha-r1"
    pr = PRContext(
        repo="owner/repo",
        pr_number=12,
        head_sha="sha-r1",
        base_sha="sha-base",
        title="R1 round trip",
        author="dev",
        html_url="https://example.test/pull/12",
        diff_url="https://example.test/pull/12.diff",
        changed_files=[],
    )
    report = _ReportAgent(trace_id)
    critic = _ReviewCritic()
    pipeline = CodeReviewPipeline(
        github_client=_PipelineGitHub(),
        review_critic=critic,
        critic_enabled=True,
    )
    empty_payload = '{"findings":[],"summary":"ok","recommendation":"approve"}'
    pipeline._triage = _StaticAgent(empty_payload)
    pipeline._static = _StaticAgent(empty_payload)
    pipeline._semantic = _StaticAgent(empty_payload)
    pipeline._test = _StaticAgent(empty_payload)
    pipeline._report = report

    class _Notifier:
        is_configured = True

        async def send_finding_cards(self, **kwargs):
            assert any(
                item.status == "published"
                and item.revision_of is not None
                and item.critic_decision == "revise"
                for item in kwargs["findings"]
            )
            return 1

        @staticmethod
        def expected_finding_card_count(findings):
            return 1 if findings else 0

        async def send_publish_control(self, **kwargs):
            return True

    store = ReviewStore()
    identity = (pr.repo, pr.pr_number, pr.head_sha)
    # This is the state TaskWorker.claim_task establishes before processor runs.
    monkeypatch.setattr(
        "apps.code_review_pipeline.agents.code_review_pipeline.CodeReviewPipeline.from_env",
        classmethod(lambda cls: pipeline),
    )
    monkeypatch.setattr(worker_module, "build_review_store", lambda: store)
    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_agent_rows",
        lambda *args, **kwargs: _async_none(),
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.notifications.feishu_notifier.FeishuReviewNotifier.from_env",
        classmethod(lambda cls: _Notifier()),
    )
    monkeypatch.setattr(feishu.settings, "code_review_finding_feedback_enabled", True)
    monkeypatch.setattr(feishu.settings, "code_review_publish_dry_run", False)
    monkeypatch.setattr(feishu, "_get_review_store", lambda: store)

    async def _safe_send(*args, **kwargs):
        return None

    monkeypatch.setattr(feishu, "_safe_send", _safe_send)

    async def _noop_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "apps.code_review_pipeline.approval.record_human_decision",
        _noop_audit,
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.approval.record_lifecycle",
        _noop_audit,
    )

    posted: list[str] = []

    class _GitHub:
        async def list_reviews(self, repo, pr_number):
            return []

        async def create_review(self, repo, pr_number, body, *, event="COMMENT"):
            assert event == "COMMENT"
            posted.append(body)
            return {"id": 991}

    monkeypatch.setattr(feishu, "build_github_client", lambda: _GitHub())

    # The task row is created by the webhook path; TaskWorker then claims it.
    store.save(
        ReviewRecord(
            trace_id=trace_id,
            repo=pr.repo,
            pr_number=pr.pr_number,
            head_sha=pr.head_sha,
            author=pr.author,
            findings_count=1,
            need_human_review=True,
            raw={},
        )
    )
    assert store.claim_task(identity)

    outcome = await worker_module.review_processor(
        TaskMessage(
            message_id="1-0",
            repo=pr.repo,
            pr_number=pr.pr_number,
            head_sha=pr.head_sha,
            task_key="owner/repo#12@sha-r1",
        )
    )
    assert outcome == "waiting_approval"
    stored_findings = store.list_findings(trace_id)
    published = tuple(item for item in stored_findings if item.status == "published")
    assert len(published) == 1
    assert published[0].revision_of
    assert published[0].critic_decision == "revise"
    assert critic.calls == 1
    assert report.calls == 2
    assert store.feedback_status(trace_id).complete is False
    review_run_id = store.get(trace_id).review_run_id
    assert review_run_id

    value = {
        "trace_id": trace_id,
        "head_sha": pr.head_sha,
        "review_run_id": review_run_id,
        "finding_uid": published[0].finding_uid,
        "disposition": "accepted_actionable",
        "reason_code": "",
    }
    await feishu._process_review_card_action(
        action="review_finding_feedback",
        value=value,
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-feedback-1",
    )
    # Feishu may redeliver the same callback; it must not create another event.
    await feishu._process_review_card_action(
        action="review_finding_feedback",
        value=value,
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-feedback-1",
    )
    assert store.feedback_status(trace_id).complete is True

    publish_value = {
        "trace_id": trace_id,
        "head_sha": pr.head_sha,
        "review_run_id": review_run_id,
    }
    await feishu._process_review_card_action(
        action="review_publish",
        value=publish_value,
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-publish-1",
    )
    await feishu._process_review_card_action(
        action="review_publish",
        value=publish_value,
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-publish-2",
    )

    assert len(posted) == 1
    assert "Wrong retry boundary" in posted[0]
    assert store.get_posted_review_id(identity) == "991"


async def _async_none():
    return None
