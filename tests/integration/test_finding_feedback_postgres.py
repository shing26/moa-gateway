from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace

import pytest

psycopg = pytest.importorskip("psycopg")

from apps.code_review_pipeline.schemas.feedback import (
    FindingFeedback,
    persisted_finding_from_result,
)
from apps.code_review_pipeline.schemas.pipeline import Finding
from apps.code_review_pipeline.storage.review_store import (
    PostgresReviewStore,
    ReviewRecord,
    StaleReviewTrace,
    _ensure_schema,
    build_review_store,
)
from tests.contracts.review_store_contract import assert_review_store_contract

DSN = "postgresql://gateway:gateway@localhost:5433/gateway"


@pytest.fixture
def pg_store() -> Iterator[PostgresReviewStore]:
    from app.config import settings

    settings.vector_db_dsn = DSN
    try:
        store = build_review_store()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 不可用（{type(exc).__name__}），跳过集成用例")
    if not isinstance(store, PostgresReviewStore):
        pytest.skip("拿到的是内存 store，说明 DSN 没生效")
    try:
        yield store
    finally:
        settings.vector_db_dsn = ""


@pytest.mark.postgres
def test_postgres_review_store_matches_shared_contract(
    pg_store: PostgresReviewStore,
) -> None:
    assert_review_store_contract(pg_store, tag=uuid.uuid4().hex[:8])


@pytest.mark.postgres
def test_review_findings_and_feedback_round_trip(
    pg_store: PostgresReviewStore,
) -> None:
    tag = uuid.uuid4().hex[:8]
    trace_id = f"cr_owner/repo#{tag}@sha"
    review_run_id = f"run-{tag}"
    record = ReviewRecord(
        trace_id=trace_id,
        repo=f"owner-{tag}/repo",
        pr_number=7,
        head_sha=f"sha-{tag}",
        author="dev",
        findings_count=1,
        need_human_review=False,
        raw={
            "base_sha": "base",
            "title": "title",
            "html_url": "https://example.test/pr",
            "diff_url": "https://example.test/pr.diff",
            "critic_metrics": {"cost_usd": 0.001, "tool_calls": 2},
        },
        review_run_id=review_run_id,
    )
    finding = persisted_finding_from_result(
        trace_id=trace_id,
        agent="report",
        finding=Finding(
            id="raw-1",
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
        evidence_verified=True,
    )
    prelabel = FindingFeedback(
        event_id=f"pre-{tag}",
        trace_id=trace_id,
        finding_uid=finding.finding_uid,
        disposition="accepted_actionable",
        reason_code="",
        actor="model:grader",
        label_source="model_prelabel",
        review_run_id=review_run_id,
    )
    human = FindingFeedback(
        event_id=f"human-{tag}",
        trace_id=trace_id,
        finding_uid=finding.finding_uid,
        disposition="accepted_actionable",
        reason_code="",
        actor="ou_reviewer",
        review_run_id=review_run_id,
    )

    pg_store.save_review(record, (finding,))
    assert pg_store.list_findings(trace_id) == (finding,)
    with pg_store._conn.cursor() as cur:
        cur.execute(
            "SELECT review_metrics FROM code_review_prs WHERE trace_id = %s",
            (trace_id,),
        )
        assert cur.fetchone()[0] == {"cost_usd": 0.001, "tool_calls": 2}
    pg_store._conn.rollback()
    pg_store.save(replace(record, raw={}))
    with pg_store._conn.cursor() as cur:
        cur.execute(
            "SELECT review_metrics FROM code_review_prs WHERE trace_id = %s",
            (trace_id,),
        )
        assert cur.fetchone()[0] == {"cost_usd": 0.001, "tool_calls": 2}
    pg_store._conn.rollback()
    assert pg_store.record_feedback(prelabel) is True
    assert pg_store.feedback_status(trace_id).complete is False
    assert pg_store.record_feedback(human) is True
    assert pg_store.record_feedback(human) is False
    assert pg_store.feedback_status(trace_id).complete is True

    # Append-only feedback may outlive a later finding snapshot. Completion must
    # count only findings that are still published in the current snapshot.
    pg_store.save_review(record, (replace(finding, status="superseded"),))
    status = pg_store.feedback_status(trace_id)
    assert status.total_findings == 0
    assert status.labelled_findings == 0
    assert status.latest_human_by_finding == {}


@pytest.mark.postgres
def test_stale_review_run_is_rejected_atomically(
    pg_store: PostgresReviewStore,
) -> None:
    tag = uuid.uuid4().hex[:8]
    repo = f"owner-{tag}/repo"
    identity = (repo, 4, "same-head")
    record = ReviewRecord(
        trace_id=f"cr_{repo}#4@same-head",
        repo=repo,
        pr_number=4,
        head_sha="same-head",
        author="dev",
        findings_count=0,
        need_human_review=False,
        raw={},
        review_run_id="run-1",
    )
    pg_store.save(record)
    assert pg_store.claim_task(identity)
    pg_store.transition_task(identity, "request_approval")
    pg_store.start_review_run(identity, "run-2")

    with pytest.raises(StaleReviewTrace, match="stale review run"):
        pg_store.transition_task(
            identity,
            "approve",
            expected_trace_id=record.trace_id,
            require_current_trace=True,
            expected_review_run_id="run-1",
        )

    assert pg_store.get_task_state(identity) == "waiting_approval"
    assert pg_store.get_review_run_id(record.trace_id) == "run-2"


@pytest.mark.postgres
def test_postgres_review_store_deduplicates_semantically_identical_findings(
    pg_store: PostgresReviewStore,
) -> None:
    tag = uuid.uuid4().hex[:8]
    trace_id = f"cr_owner/repo#{tag}@dedupe"
    record = ReviewRecord(
        trace_id=trace_id,
        repo=f"owner-{tag}/repo",
        pr_number=8,
        head_sha=f"sha-{tag}",
        author="dev",
        findings_count=2,
        need_human_review=True,
        raw={},
    )
    candidate = persisted_finding_from_result(
        trace_id=trace_id,
        agent="report",
        finding=Finding(
            id="raw-1",
            severity="high",
            category="logic",
            file="app/service.py",
            line=10,
            title="Wrong retry boundary",
            description="The last retry is skipped.",
            suggestion="Use an inclusive boundary.",
            confidence=0.9,
            evidence=("diff:app/service.py:10",),
        ),
        status="candidate",
    )
    published = replace(candidate, status="published")

    pg_store.save_review(record, (candidate, published))

    assert pg_store.list_findings(trace_id) == (published,)
    events = pg_store.list_finding_events(trace_id)
    assert [item.status for item in events] == ["candidate", "published"]

    pg_store.save_review(record, (candidate, published))
    assert pg_store.list_finding_events(trace_id) == events


@pytest.mark.postgres
def test_current_trace_uses_review_creation_order_not_reclaim_time(
    pg_store: PostgresReviewStore,
) -> None:
    tag = uuid.uuid4().hex[:8]
    repo = f"owner-{tag}/repo"
    older = ReviewRecord(
        trace_id=f"cr_{repo}#1@older",
        repo=repo,
        pr_number=1,
        head_sha="older",
        author="dev",
        findings_count=0,
        need_human_review=False,
        raw={},
    )
    newer = replace(older, trace_id=f"cr_{repo}#1@newer", head_sha="newer")

    pg_store.save(older)
    pg_store.save(newer)
    # A reclaim of the older SHA must not make its card current again.
    pg_store.save(older)

    assert pg_store.current_trace_id(repo, 1) == newer.trace_id


@pytest.mark.postgres
def test_stale_trace_transition_is_rejected_atomically(
    pg_store: PostgresReviewStore,
) -> None:
    tag = uuid.uuid4().hex[:8]
    repo = f"owner-{tag}/repo"
    identity = (repo, 3, "older")
    older = ReviewRecord(
        trace_id=f"cr_{repo}#3@older",
        repo=repo,
        pr_number=3,
        head_sha="older",
        author="dev",
        findings_count=0,
        need_human_review=False,
        raw={},
    )
    newer = replace(older, trace_id=f"cr_{repo}#3@newer", head_sha="newer")
    pg_store.save(older)
    assert pg_store.claim_task(identity)
    pg_store.transition_task(identity, "request_approval")
    pg_store.save(newer)

    with pytest.raises(StaleReviewTrace):
        pg_store.transition_task(
            identity,
            "approve",
            expected_trace_id=older.trace_id,
            require_current_trace=True,
            metadata={"operator": "alice"},
        )
    assert pg_store.get_task_state(identity) == "waiting_approval"


@pytest.mark.postgres
def test_legacy_finding_rows_are_excluded_from_feedback(
    pg_store: PostgresReviewStore,
) -> None:
    tag = uuid.uuid4().hex[:8]
    trace_id = f"cr_owner/repo#{tag}@legacy"
    record = ReviewRecord(
        trace_id=trace_id,
        repo=f"owner-{tag}/repo",
        pr_number=10,
        head_sha=f"sha-{tag}",
        author="dev",
        findings_count=1,
        need_human_review=True,
        raw={},
    )
    pg_store.save(record)
    with pg_store._conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO code_review_findings (
                trace_id, agent, finding_id, severity, category, file, line,
                title, description, suggestion, confidence, team_specific, evidence
            ) VALUES (%s, 'report', 'legacy-1', 'high', 'logic', 'app/x.py', 1,
                      'legacy', 'legacy finding', 'none', 0.5, false, '{}')
            RETURNING id
            """,
            (trace_id,),
        )
        legacy_id = int(cur.fetchone()[0])
    pg_store._conn.commit()

    _ensure_schema(DSN)

    with pg_store._conn.cursor() as cur:
        cur.execute(
            "SELECT finding_uid, fingerprint, status FROM code_review_findings WHERE id = %s",
            (legacy_id,),
        )
        finding_uid, fingerprint, status = cur.fetchone()
    pg_store._conn.rollback()

    assert finding_uid == f"legacy:{legacy_id}"
    assert fingerprint == f"legacy:{legacy_id}"
    assert status == "legacy"
    assert pg_store.feedback_status(trace_id).total_findings == 0
