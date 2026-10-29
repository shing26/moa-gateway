from __future__ import annotations

import pytest

from tests.contracts.review_store_contract import assert_review_store_contract
from apps.code_review_pipeline.schemas.feedback import (
    FindingFeedback,
    persisted_finding_from_result,
)
from apps.code_review_pipeline.schemas.pipeline import Finding
from apps.code_review_pipeline.storage.review_store import ReviewRecord, ReviewStore


def _finding(*, line: int = 10, evidence: tuple[str, ...] = ("b", "a")) -> Finding:
    return Finding(
        id="raw-1",
        severity="high",
        category="logic",
        file="app/service.py",
        line=line,
        title="Wrong retry boundary",
        description="The last retry is skipped.",
        suggestion="Use an inclusive boundary.",
        confidence=0.9,
        evidence=evidence,
        source_agents=("semantic_review", "report"),
    )


def test_finding_identity_is_stable_across_evidence_order_location_and_word_order() -> None:
    first = persisted_finding_from_result(
        trace_id="cr_owner/repo#7@abc",
        agent="report",
        finding=_finding(evidence=("raw output", "diff:app/service.py:10")),
    )
    reordered = persisted_finding_from_result(
        trace_id="cr_owner/repo#7@abc",
        agent="report",
        finding=_finding(evidence=("diff:app/service.py:10", "raw output")),
    )
    moved = persisted_finding_from_result(
        trace_id="cr_owner/repo#7@abc",
        agent="report",
        finding=_finding(line=11),
    )
    reworded = persisted_finding_from_result(
        trace_id="cr_owner/repo#7@abc",
        agent="report",
        finding=Finding(
            id="raw-2",
            severity="high",
            category="LOGIC",
            file="./app/service.py",
            line=99,
            title="Retry boundary is wrong",
            description="Retry: the last one is skipped!",
            suggestion="Use an inclusive boundary.",
            confidence=0.9,
            source_agents=("semantic_review", "report"),
        ),
    )

    assert first.finding_uid == reordered.finding_uid
    assert first.fingerprint == reordered.fingerprint
    assert first.finding_uid == moved.finding_uid
    assert first.fingerprint == moved.fingerprint
    assert first.finding_uid != reworded.finding_uid


def test_false_positive_feedback_requires_a_reason_code() -> None:
    with pytest.raises(ValueError, match="reason_code"):
        FindingFeedback(
            event_id="evt-1",
            trace_id="cr_owner/repo#7@abc",
            finding_uid="finding-1",
            disposition="false_positive",
            reason_code="",
            actor="ou_reviewer",
            label_source="human",
        )


def test_memory_review_store_matches_shared_contract() -> None:
    assert_review_store_contract(ReviewStore(), tag="memory")


def test_review_store_round_trips_persisted_findings_with_the_same_uid() -> None:
    store = ReviewStore()
    record = ReviewRecord(
        trace_id="cr_owner/repo#7@abc",
        repo="owner/repo",
        pr_number=7,
        head_sha="abc",
        author="dev",
        findings_count=1,
        need_human_review=True,
        raw={},
    )
    persisted = persisted_finding_from_result(
        trace_id=record.trace_id,
        agent="report",
        finding=_finding(),
    )

    store.save_review(record, (persisted,))

    assert store.get(record.trace_id) == record
    assert store.list_findings(record.trace_id) == (persisted,)


def test_review_store_deduplicates_semantically_identical_findings() -> None:
    store = ReviewStore()
    record = ReviewRecord(
        trace_id="cr_owner/repo#7@dedupe",
        repo="owner/repo",
        pr_number=7,
        head_sha="dedupe",
        author="dev",
        findings_count=2,
        need_human_review=True,
        raw={},
    )
    candidate = persisted_finding_from_result(
        trace_id=record.trace_id,
        agent="report",
        finding=_finding(),
        status="candidate",
    )
    published = persisted_finding_from_result(
        trace_id=record.trace_id,
        agent="report",
        finding=_finding(),
        status="published",
    )

    store.save_review(record, (candidate, published))

    assert store.list_findings(record.trace_id) == (published,)
    events = store.list_finding_events(record.trace_id)
    assert [item.status for item in events] == ["candidate", "published"]

    changed_candidate = persisted_finding_from_result(
        trace_id=record.trace_id,
        agent="report",
        finding=_finding(evidence=("diff:app/service.py:10",)),
        status="candidate",
    )
    store.save_review(record, (changed_candidate, published))
    assert store.list_finding_events(record.trace_id) == events


def test_feedback_is_idempotent_by_event_and_completion_counts_only_human_labels() -> None:
    store = ReviewStore()
    record = ReviewRecord(
        trace_id="cr_owner/repo#8@def",
        repo="owner/repo",
        pr_number=8,
        head_sha="def",
        author="dev",
        findings_count=1,
        need_human_review=False,
        raw={},
    )
    finding = persisted_finding_from_result(
        trace_id=record.trace_id,
        agent="report",
        finding=_finding(),
    )
    store.save_review(record, (finding,))
    prelabel = FindingFeedback(
        event_id="evt-prelabel",
        trace_id=record.trace_id,
        finding_uid=finding.finding_uid,
        disposition="accepted_actionable",
        reason_code="",
        actor="model:grader",
        label_source="model_prelabel",
    )
    human = FindingFeedback(
        event_id="evt-human",
        trace_id=record.trace_id,
        finding_uid=finding.finding_uid,
        disposition="accepted_actionable",
        reason_code="",
        actor="ou_reviewer",
    )

    assert store.record_feedback(prelabel) is True
    assert store.feedback_status(record.trace_id).complete is False
    assert store.record_feedback(human) is True
    assert store.record_feedback(human) is False
    status = store.feedback_status(record.trace_id)
    assert status.complete is True
    assert status.labelled_findings == 1


def test_latest_human_feedback_uses_event_id_as_a_tie_breaker() -> None:
    store = ReviewStore()
    record = ReviewRecord(
        trace_id="cr_owner/repo#8@tie",
        repo="owner/repo",
        pr_number=8,
        head_sha="tie",
        author="dev",
        findings_count=1,
        need_human_review=False,
        raw={},
    )
    finding = persisted_finding_from_result(
        trace_id=record.trace_id,
        agent="report",
        finding=_finding(),
    )
    store.save_review(record, (finding,))
    created_at = "2026-10-20T00:00:00+00:00"
    first = FindingFeedback(
        event_id="evt-a",
        trace_id=record.trace_id,
        finding_uid=finding.finding_uid,
        disposition="accepted_actionable",
        reason_code="",
        actor="reviewer",
        created_at=created_at,
    )
    second = FindingFeedback(
        event_id="evt-z",
        trace_id=record.trace_id,
        finding_uid=finding.finding_uid,
        disposition="duplicate",
        reason_code="",
        actor="reviewer",
        created_at=created_at,
    )

    assert store.record_feedback(first) is True
    assert store.record_feedback(second) is True
    assert (
        store.feedback_status(record.trace_id)
        .latest_human_by_finding[finding.finding_uid]
        .disposition
        == "duplicate"
    )


def test_feedback_is_scoped_to_the_current_review_run() -> None:
    store = ReviewStore()
    record = ReviewRecord(
        trace_id="cr_owner/repo#8@run",
        repo="owner/repo",
        pr_number=8,
        head_sha="run",
        author="dev",
        findings_count=1,
        need_human_review=False,
        raw={},
        review_run_id="run-2",
    )
    finding = persisted_finding_from_result(
        trace_id=record.trace_id,
        agent="report",
        finding=_finding(),
    )
    store.save_review(record, (finding,))
    stale = FindingFeedback(
        event_id="evt-stale-run",
        trace_id=record.trace_id,
        finding_uid=finding.finding_uid,
        disposition="accepted_actionable",
        reason_code="",
        actor="reviewer",
        review_run_id="run-1",
    )

    with pytest.raises(ValueError, match="stale review run"):
        store.record_feedback(stale)
