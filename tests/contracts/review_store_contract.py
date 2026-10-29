from __future__ import annotations

from dataclasses import replace
from typing import Any

from apps.code_review_pipeline.schemas.feedback import (
    FindingFeedback,
    persisted_finding_from_result,
)
from apps.code_review_pipeline.schemas.pipeline import Finding
from apps.code_review_pipeline.storage.review_store import ReviewRecord


def assert_review_store_contract(store: Any, *, tag: str) -> None:
    """Exercise the memory and PostgreSQL ReviewStore implementations identically."""
    repo = f"contract-{tag}/repo"
    trace_id = f"cr_{repo}#11@{tag}"
    run_id = f"run-{tag}"
    record = ReviewRecord(
        trace_id=trace_id,
        repo=repo,
        pr_number=11,
        head_sha=tag,
        author="dev",
        findings_count=1,
        need_human_review=False,
        raw={},
        review_run_id=run_id,
    )
    finding = persisted_finding_from_result(
        trace_id=trace_id,
        agent="report",
        finding=Finding(
            id="raw-contract-1",
            severity="high",
            category="logic",
            file="app/service.py",
            line=10,
            title="Wrong retry boundary",
            description="The last retry is skipped.",
            suggestion="Use an inclusive boundary.",
            confidence=0.9,
            evidence=("diff:app/service.py:10",),
            source_agents=("semantic_review", "report"),
        ),
        status="candidate",
        evidence_verified=True,
    )

    store.save_review(record, (finding,))
    stored = store.get(trace_id)
    assert stored is not None
    assert (
        stored.trace_id,
        stored.repo,
        stored.pr_number,
        stored.head_sha,
        stored.review_run_id,
    ) == (
        record.trace_id,
        record.repo,
        record.pr_number,
        record.head_sha,
        record.review_run_id,
    )
    assert store.list_findings(trace_id) == (finding,)

    candidate = finding
    published = replace(finding, status="published")
    store.save_review(record, (candidate, published))
    assert store.list_findings(trace_id) == (published,)
    assert [event.status for event in store.list_finding_events(trace_id)] == [
        "candidate",
        "published",
    ]

    prelabel = FindingFeedback(
        event_id=f"model-{tag}",
        trace_id=trace_id,
        finding_uid=finding.finding_uid,
        disposition="accepted_actionable",
        reason_code="",
        actor="model:grader",
        label_source="model_prelabel",
        review_run_id=run_id,
    )
    human = replace(
        prelabel,
        event_id=f"human-{tag}",
        actor="ou_reviewer",
        label_source="human",
    )
    assert store.record_feedback(prelabel) is True
    assert store.feedback_status(trace_id).complete is False
    assert store.record_feedback(human) is True
    assert store.record_feedback(human) is False
    status = store.feedback_status(trace_id)
    assert status.complete is True
    latest = status.latest_human_by_finding[finding.finding_uid]
    assert (
        latest.event_id,
        latest.trace_id,
        latest.finding_uid,
        latest.disposition,
        latest.actor,
        latest.label_source,
        latest.review_run_id,
    ) == (
        human.event_id,
        human.trace_id,
        human.finding_uid,
        human.disposition,
        human.actor,
        human.label_source,
        human.review_run_id,
    )

    identity = (record.repo, record.pr_number, record.head_sha)
    assert store.claim_task(identity) is True
    store.transition_task(identity, "request_approval")
    store.transition_task(identity, "approve")
    assert store.claim_publish_attempt(
        identity,
        owner="owner-1",
        lease_seconds=60,
        expected_review_run_id=run_id,
    )
    assert not store.claim_publish_attempt(
        identity,
        owner="owner-2",
        lease_seconds=60,
        expected_review_run_id=run_id,
    )
    store.release_publish_attempt(identity, owner="owner-1")
    assert store.claim_publish_attempt(
        identity,
        owner="owner-2",
        lease_seconds=60,
        expected_review_run_id=run_id,
    )
