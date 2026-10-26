from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.code_review_pipeline.change.bundle import ChangeBundle, FileChange
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.delivery.audit import WalDeliveryAudit
from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    AgentAttempt,
    AgentStep,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    EvidenceRef,
    FailureCode,
    FailureInfo,
    PushOutcome,
    VerificationEvidence,
    VerifierVerdict,
)
from apps.code_review_pipeline.delivery.run_store import DeliveryRunStore
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore


def _run(state: DeliveryRunState = DeliveryRunState.READY_FOR_PUSH_APPROVAL) -> DeliveryRun:
    request = DeliveryRequest(
        trace_id="trace-1",
        repo="owner/name",
        base_sha="a" * 40,
        base_branch="main",
        branch="moa/fix",
        goal="fix",
        title="fix",
        body="",
    )
    now = datetime(2026, 10, 27, tzinfo=UTC)
    return DeliveryRun(
        schema_version=SCHEMA_VERSION,
        run_id="run-1",
        trace_id="trace-1",
        request=request,
        topology="single",
        state=state,
        attempts=(),
        verification=None,
        verifier_verdicts=(),
        bundle=None,
        push=None,
        merge=None,
        usage=DeliveryUsage(),
        failure=None,
        started_at=now,
        updated_at=now,
    )


def _full_run() -> DeliveryRun:
    base = _run(DeliveryRunState.NEEDS_HUMAN)
    attempt = AgentAttempt(
        role="implementer",
        status="completed",
        summary="changed app.py",
        stop_reason="model finished",
        usage=DeliveryUsage(
            llm_calls=2,
            prompt_tokens=30,
            completion_tokens=10,
            cost_usd=0.02,
            llm_latency_ms=12.5,
        ),
        steps=(
            AgentStep(
                index=1,
                action="call_tool",
                tool="write_file",
                arguments={"path": "app.py"},
                observation="written",
                outcome="ok",
                elapsed_ms=1.5,
            ),
        ),
    )
    bundle = ChangeBundle(
        base_sha="a" * 40,
        branch="moa/fix-trace-1",
        title="fix",
        body="body",
        files=(
            FileChange(
                path="app.py",
                kind="modified",
                before_hash="1" * 64,
                after_hash="2" * 64,
            ),
        ),
        patch="diff --git a/app.py b/app.py\n",
        patch_hash="3" * 64,
        verify_command=("pytest", "-q"),
        verify_exit_code=0,
        verify_output="1 passed",
        model="test-model",
        trace_id="trace-1",
    )
    verification = VerificationEvidence(
        command=("pytest", "-q"),
        exit_code=0,
        output_hash="4" * 64,
        output_excerpt="1 passed",
    )
    push = PushOutcome(
        branch=bundle.branch,
        commit_sha="b" * 40,
        pull_request=PullRequestRef(
            number=9,
            url="https://example.com/pr/9",
            head_sha="b" * 40,
        ),
    )
    merge = MergeRecord(
        repo="owner/name",
        pr_number=9,
        head_sha="b" * 40,
        state=MergeState.CI_FAILED.value,
        ci_state="failure",
        ci_failing=({"name": "unit", "url": "https://example.com/check/1"},),
        card_message_id="card-1",
        failure_reason="unit failed",
        transitions=(
            {"from": "watching", "to": "ci_failed", "action": "ci_red"},
        ),
    )
    verdict = VerifierVerdict(
        decision="revise",
        reason="missing edge case",
        instructions="add the zero-input test and fix the branch",
        evidence=(
            EvidenceRef(
                kind="file",
                ref="app.py",
                observation="the zero branch is absent",
            ),
        ),
        reviewed_bundle_hash=bundle.bundle_hash,
        reviewed_patch_hash=bundle.patch_hash,
        attempt=AgentAttempt(
            role="verifier",
            status="completed",
            summary="needs revision",
            stop_reason="model finished",
            usage=DeliveryUsage(llm_calls=1, prompt_tokens=20, completion_tokens=5),
            steps=(),
        ),
    )
    return replace(
        base,
        attempts=(attempt,),
        verification=verification,
        verifier_verdicts=(verdict,),
        bundle=bundle,
        push=push,
        merge=merge,
        usage=attempt.usage + verdict.attempt.usage,
        failure=FailureInfo(
            FailureCode.VERIFIER_REVISE_EXHAUSTED,
            "revision budget exhausted",
        ),
    )


@pytest.mark.asyncio
async def test_run_store_round_trips_every_delivery_evidence_field(
    tmp_path: Path,
) -> None:
    store = DeliveryRunStore(tmp_path)
    original = _full_run()

    await store.save(original)

    assert store.load(original.run_id) == original


@pytest.mark.asyncio
async def test_run_store_writes_a_readable_snapshot(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    original = _run()

    await store.save(original)
    loaded = store.load("run-1")

    assert loaded == original
    assert (tmp_path / "run-1.json").is_file()


@pytest.mark.asyncio
async def test_run_store_replaces_atomically_without_partial_json(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    await store.save(_run())
    await store.save(replace(_run(), state=DeliveryRunState.WAITING_CI))

    loaded = store.load("run-1")

    assert loaded.state is DeliveryRunState.WAITING_CI
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_current_view_merges_the_latest_merge_record(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    merges = MergeStore()
    record = MergeRecord(
        repo="owner/name",
        pr_number=9,
        head_sha="b" * 40,
        state=MergeState.AWAITING_APPROVAL.value,
    )
    merges.save(record)
    run = replace(
        _run(DeliveryRunState.WAITING_CI),
        merge=record,
    )

    view = store.view(run, merges)

    assert view.merge == record
    assert view.state is DeliveryRunState.WAITING_CI


@pytest.mark.asyncio
async def test_audit_adapter_writes_transition_fields(monkeypatch, tmp_path: Path) -> None:
    entries = []

    async def capture(entry):
        entries.append(entry)

    monkeypatch.setattr("apps.code_review_pipeline.delivery.audit.record_audit", capture)
    audit = WalDeliveryAudit()
    previous = DeliveryRunState.IMPLEMENTING
    current = DeliveryRunState.CHECKING

    await audit.record(
        run=_run(current),
        previous=previous,
        current=current,
        detail={"action": "verify"},
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.trace_id == "trace-1"
    assert entry.agent_name == "delivery_lifecycle"
    assert entry.extra["run_id"] == "run-1"
    assert entry.extra["from_state"] == "implementing"
    assert entry.extra["to_state"] == "checking"
    assert entry.extra["action"] == "verify"


@pytest.mark.asyncio
async def test_audit_adapter_keeps_canonical_transition_fields_when_detail_collides(
    monkeypatch,
) -> None:
    entries = []

    async def capture(entry):
        entries.append(entry)

    monkeypatch.setattr("apps.code_review_pipeline.delivery.audit.record_audit", capture)
    audit = WalDeliveryAudit()
    previous = DeliveryRunState.IMPLEMENTING
    current = DeliveryRunState.CHECKING

    await audit.record(
        run=_run(current),
        previous=previous,
        current=current,
        detail={
            "run_id": "forged-run",
            "from_state": "forged-from",
            "to_state": "forged-to",
            "action": "verify",
        },
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.extra["run_id"] == "run-1"
    assert entry.extra["from_state"] == "implementing"
    assert entry.extra["to_state"] == "checking"
    assert entry.extra["action"] == "verify"
