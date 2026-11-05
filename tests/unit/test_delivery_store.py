from __future__ import annotations

import json
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


def _pushed_run(
    state: DeliveryRunState = DeliveryRunState.WAITING_CI,
) -> DeliveryRun:
    return replace(
        _run(state),
        push=PushOutcome(
            branch="moa/fix-trace-1",
            commit_sha="b" * 40,
            pull_request=PullRequestRef(
                number=9,
                url="https://example.com/pr/9",
                head_sha="b" * 40,
            ),
        ),
    )


def _merge_record(**overrides: object) -> MergeRecord:
    values: dict[str, object] = {
        "repo": "owner/name",
        "pr_number": 9,
        "head_sha": "b" * 40,
        "run_id": "run-1",
        "trace_id": "trace-1",
    }
    values.update(overrides)
    return MergeRecord(**values)


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
async def test_run_store_round_trips_file_modes(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    original = _full_run()
    assert original.bundle is not None
    original = replace(
        original,
        bundle=replace(
            original.bundle,
            files=(
                replace(original.bundle.files[0], mode="100755"),
            ),
        ),
    )

    await store.save(original)

    assert store.load(original.run_id) == original


@pytest.mark.asyncio
async def test_run_store_defaults_legacy_file_mode_to_regular(
    tmp_path: Path,
) -> None:
    store = DeliveryRunStore(tmp_path)
    original = _full_run()
    assert original.bundle is not None
    await store.save(original)
    path = tmp_path / f"{original.run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["bundle"]["files"][0]["mode"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load(original.run_id)

    assert loaded.bundle is not None
    assert loaded.bundle.files[0].mode == "100644"


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
    initial = MergeRecord(
        repo="owner/name",
        pr_number=9,
        head_sha="b" * 40,
        state=MergeState.WATCHING.value,
    )
    merges.save(initial)
    run = replace(
        _run(DeliveryRunState.WAITING_CI),
        push=PushOutcome(
            branch="moa/fix-trace-1",
            commit_sha="b" * 40,
            pull_request=PullRequestRef(
                number=9,
                url="https://example.com/pr/9",
                head_sha="b" * 40,
            ),
        ),
        merge=initial,
    )
    latest = replace(
        initial,
        state=MergeState.AWAITING_APPROVAL.value,
        ci_state="success",
    )
    merges.save(latest)

    view = store.view(run, merges)

    assert view.merge == latest
    assert view.merge != initial
    assert view.state is DeliveryRunState.WAITING_CI


@pytest.mark.asyncio
async def test_sync_from_merge_writes_merged_state_to_snapshot(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    await store.save(_pushed_run())
    record = _merge_record(state=MergeState.MERGED.value, merged_sha="c" * 40)

    synced = await store.sync_from_merge(record)

    assert synced is not None
    assert synced.state is DeliveryRunState.MERGED
    assert synced.merge == record
    loaded = store.load("run-1")
    assert loaded.state is DeliveryRunState.MERGED
    assert loaded.merge == record
    assert loaded.updated_at > loaded.started_at


@pytest.mark.asyncio
async def test_sync_from_merge_maps_awaiting_approval(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    await store.save(_pushed_run())
    record = _merge_record(state=MergeState.AWAITING_APPROVAL.value)

    synced = await store.sync_from_merge(record)

    assert synced is not None
    assert synced.state is DeliveryRunState.AWAITING_MERGE_APPROVAL


@pytest.mark.asyncio
async def test_sync_from_merge_failure_marks_needs_human(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    await store.save(_pushed_run())
    record = _merge_record(
        state=MergeState.FAILED.value,
        failure_reason="PR is not mergeable (dirty)",
    )

    synced = await store.sync_from_merge(record)

    assert synced is not None
    assert synced.state is DeliveryRunState.NEEDS_HUMAN
    assert synced.failure == FailureInfo(
        FailureCode.MERGE_FAILED,
        "PR is not mergeable (dirty)",
    )


@pytest.mark.asyncio
async def test_sync_from_merge_falls_back_to_pull_request_identity(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    await store.save(_pushed_run())
    record = _merge_record(run_id="", state=MergeState.MERGED.value)

    synced = await store.sync_from_merge(record)

    assert synced is not None
    assert synced.state is DeliveryRunState.MERGED


@pytest.mark.asyncio
async def test_sync_from_merge_ignores_another_pull_request(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    await store.save(_pushed_run())
    record = _merge_record(run_id="", head_sha="d" * 40)

    assert await store.sync_from_merge(record) is None


@pytest.mark.asyncio
async def test_sync_from_merge_never_reopens_terminal_run(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    merged = _merge_record(state=MergeState.MERGED.value, merged_sha="c" * 40)
    run = replace(_pushed_run(), state=DeliveryRunState.MERGED, merge=merged)
    await store.save(run)
    late = _merge_record(state=MergeState.WATCHING.value)

    synced = await store.sync_from_merge(late)

    assert synced is not None
    assert synced.state is DeliveryRunState.MERGED
    assert synced.merge == merged
    loaded = store.load("run-1")
    assert loaded.merge == merged


@pytest.mark.asyncio
async def test_sync_from_merge_folds_needs_human_into_merged(
    monkeypatch, tmp_path: Path
) -> None:
    store = DeliveryRunStore(tmp_path)
    merged = _merge_record(state=MergeState.MERGED.value, merged_sha="c" * 40)
    boom = "boom"
    failure = FailureInfo(FailureCode.INFRA_ERROR, boom)
    run = replace(
        _pushed_run(state=DeliveryRunState.NEEDS_HUMAN),
        merge=merged,
        failure=failure,
    )
    await store.save(run)

    entries = []

    async def capture(entry):
        entries.append(entry)

    monkeypatch.setattr("apps.code_review_pipeline.delivery.audit.record_audit", capture)
    audit = WalDeliveryAudit()

    synced = await store.sync(run, merged, audit=audit)

    assert synced.state is DeliveryRunState.MERGED
    assert synced.failure is failure
    assert synced.merge == merged
    assert len(entries) == 1
    entry = entries[0]
    assert entry.extra["action"] == "merge_sync"
    assert entry.extra["merge_state"] == "merged"
    assert entry.extra["pr"] == merged.task_key
    assert entry.extra["merged_sha"] == merged.merged_sha
    assert entry.extra["folded_from"] == "needs_human"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        DeliveryRunState.REJECTED,
        DeliveryRunState.FAILED,
        DeliveryRunState.BUDGET_EXHAUSTED,
    ],
)
async def test_sync_from_merge_keeps_terminal_run_unchanged_when_merged(
    monkeypatch, tmp_path: Path, state: DeliveryRunState
) -> None:
    store = DeliveryRunStore(tmp_path)
    merged = _merge_record(state=MergeState.MERGED.value, merged_sha="c" * 40)
    run = replace(_pushed_run(state=state), merge=merged)
    await store.save(run)

    entries = []

    async def capture(entry):
        entries.append(entry)

    monkeypatch.setattr("apps.code_review_pipeline.delivery.audit.record_audit", capture)
    audit = WalDeliveryAudit()

    synced = await store.sync(run, merged, audit=audit)

    assert synced is run
    assert synced.state is state
    assert synced.merge == merged
    assert entries == []


@pytest.mark.asyncio
async def test_reconcile_persists_latest_merge_record(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    run = _pushed_run()
    await store.save(run)
    merges = MergeStore()
    record = _merge_record(state=MergeState.MERGED.value, merged_sha="c" * 40)
    merges.save(record)

    reconciled = await store.reconcile(run, merges)

    assert reconciled.state is DeliveryRunState.MERGED
    assert store.load("run-1").merge == record


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
