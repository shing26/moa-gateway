from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.code_review_pipeline.change.bundle import ChangeBundle, FileChange
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.delivery import abandon as abandon_module
from apps.code_review_pipeline.delivery.abandon import abandon_run
from apps.code_review_pipeline.delivery.lockfile import LockInfo, StaleLockReplaced
from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    AgentAttempt,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    FailureCode,
    FailureInfo,
    PushOutcome,
    VerificationEvidence,
)
from apps.code_review_pipeline.delivery.run_store import DeliveryRunStore
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore


def _request() -> DeliveryRequest:
    return DeliveryRequest(
        trace_id="trace-1",
        repo="owner/name",
        base_sha="a" * 40,
        base_branch="main",
        branch="moa/fix",
        goal="fix",
        title="fix",
        body="",
    )


def _bundle() -> ChangeBundle:
    return ChangeBundle(
        base_sha="a" * 40,
        branch="moa/fix-trace-1",
        title="fix",
        body="",
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


def _push() -> PushOutcome:
    return PushOutcome(
        branch="moa/fix-trace-1",
        commit_sha="b" * 40,
        pull_request=PullRequestRef(
            number=9,
            url="https://example.com/pr/9",
            head_sha="b" * 40,
        ),
    )


def _run(
    state: DeliveryRunState = DeliveryRunState.WAITING_CI,
    *,
    run_id: str = "run-1",
    push: PushOutcome | None = None,
    bundle: ChangeBundle | None = None,
    verification: VerificationEvidence | None = None,
    attempts: tuple[AgentAttempt, ...] = (),
    failure: FailureInfo | None = None,
) -> DeliveryRun:
    now = datetime(2026, 10, 27, tzinfo=UTC)
    return DeliveryRun(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        trace_id="trace-1",
        request=_request(),
        topology="single",
        state=state,
        attempts=attempts,
        verification=verification,
        verifier_verdicts=(),
        bundle=bundle,
        push=push,
        merge=None,
        usage=DeliveryUsage(),
        failure=failure,
        started_at=now,
        updated_at=now,
    )


def _pushed_run(
    state: DeliveryRunState = DeliveryRunState.WAITING_CI,
    **kwargs: object,
) -> DeliveryRun:
    values: dict[str, object] = {"push": _push()}
    values.update(kwargs)
    return _run(state, **values)  # type: ignore[arg-type]


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        delivery_run_record_dir=str(tmp_path / "runs"),
    )


def _save(tmp_path: Path, run: DeliveryRun) -> DeliveryRunStore:
    store = DeliveryRunStore(tmp_path / "runs")
    asyncio.run(store.save(run))
    return store


def _record_bytes(tmp_path: Path, run_id: str = "run-1") -> bytes:
    return (tmp_path / "runs" / f"{run_id}.json").read_bytes()


def _merge_record(state: MergeState, **overrides: object) -> MergeRecord:
    values: dict[str, object] = {
        "repo": "owner/name",
        "pr_number": 9,
        "head_sha": "b" * 40,
        "state": state.value,
        "run_id": "run-1",
        "trace_id": "trace-1",
    }
    values.update(overrides)
    return MergeRecord(**values)  # type: ignore[arg-type]


class _Audit:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def record(
        self,
        *,
        run: DeliveryRun,
        previous: DeliveryRunState | None,
        current: DeliveryRunState,
        detail: dict[str, object],
    ) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append(
            {
                "run": run,
                "previous": previous,
                "current": current,
                "detail": detail,
            }
        )


def test_t1_abandon_from_waiting_ci_writes_terminal_record_and_keeps_evidence(
    tmp_path: Path,
) -> None:
    verification = VerificationEvidence(
        command=("pytest", "-q"),
        exit_code=0,
        output_hash="4" * 64,
        output_excerpt="1 passed",
    )
    attempt = AgentAttempt(
        role="implementer",
        status="completed",
        summary="changed app.py",
        stop_reason="model finished",
        usage=DeliveryUsage(llm_calls=1),
        steps=(),
    )
    run = _pushed_run(
        bundle=_bundle(),
        verification=verification,
        attempts=(attempt,),
    )
    store = _save(tmp_path, run)
    audit = _Audit()

    outcome = asyncio.run(
        abandon_run(
            "run-1",
            store=store,
            merge_store=MergeStore(),
            settings=_settings(tmp_path),
            reason="PR #110 was closed; CI will never conclude",
            audit=audit,
        )
    )

    assert outcome.exit_code == 0
    assert outcome.already is False
    assert outcome.merge_record == "none"
    loaded = store.load("run-1")
    assert loaded.state is DeliveryRunState.ABANDONED
    assert loaded.failure == FailureInfo(
        FailureCode.ABANDONED,
        "PR #110 was closed; CI will never conclude",
    )
    assert loaded.push == run.push
    assert loaded.bundle == run.bundle
    assert loaded.verification == run.verification
    assert loaded.attempts == run.attempts
    assert loaded.updated_at > run.updated_at


@pytest.mark.parametrize("reason", ["", "   "])
def test_t2_blank_reason_is_refused_without_touching_the_record(
    tmp_path: Path, reason: str
) -> None:
    run = _pushed_run()
    store = _save(tmp_path, run)
    before = _record_bytes(tmp_path)

    outcome = asyncio.run(
        abandon_run(
            "run-1",
            store=store,
            merge_store=None,
            settings=_settings(tmp_path),
            reason=reason,
            audit=_Audit(),
        )
    )

    assert outcome.exit_code == 2
    assert "reason is required" in outcome.reason
    assert _record_bytes(tmp_path) == before


@pytest.mark.parametrize(
    "state",
    [
        DeliveryRunState.MERGING,
        DeliveryRunState.MERGED,
        DeliveryRunState.REJECTED,
    ],
)
def test_t3_refused_states_leave_record_untouched(
    tmp_path: Path, state: DeliveryRunState
) -> None:
    run = _run(state)
    store = _save(tmp_path, run)
    before = _record_bytes(tmp_path)
    audit = _Audit()

    outcome = asyncio.run(
        abandon_run(
            "run-1",
            store=store,
            merge_store=MergeStore(),
            settings=_settings(tmp_path),
            reason="stop",
            audit=audit,
        )
    )

    assert outcome.exit_code == 2
    assert state.value in outcome.reason
    assert _record_bytes(tmp_path) == before
    assert audit.calls == []


def test_t3_abandoned_is_idempotent_and_does_not_write_audit(
    tmp_path: Path,
) -> None:
    run = _run(DeliveryRunState.ABANDONED)
    store = _save(tmp_path, run)
    before = _record_bytes(tmp_path)
    audit = _Audit()

    outcome = asyncio.run(
        abandon_run(
            "run-1",
            store=store,
            merge_store=MergeStore(),
            settings=_settings(tmp_path),
            reason="stop",
            audit=audit,
        )
    )

    assert outcome.exit_code == 0
    assert outcome.already is True
    assert _record_bytes(tmp_path) == before
    assert audit.calls == []


def test_t4_supersedes_the_open_merge_card_before_abandoning(
    tmp_path: Path,
) -> None:
    run = _pushed_run()
    store = _save(tmp_path, run)
    merges = MergeStore()
    merges.save(_merge_record(MergeState.AWAITING_APPROVAL))
    audit = _Audit()

    outcome = asyncio.run(
        abandon_run(
            "run-1",
            store=store,
            merge_store=merges,
            settings=_settings(tmp_path),
            reason="PR was closed",
            operator="alice",
            audit=audit,
        )
    )

    assert outcome.exit_code == 0
    assert outcome.merge_record == "superseded"
    identity = ("owner/name", 9, "b" * 40)
    assert merges.get(identity).state == MergeState.SUPERSEDED.value
    assert merges.list_open() == []
    assert store.load("run-1").state is DeliveryRunState.ABANDONED
    assert audit.calls[0]["detail"] == {
        "reason": "PR was closed",
        "operator": "alice",
        "merge_record": "superseded",
    }


def test_t5_merged_merge_record_refuses_and_changes_nothing(
    tmp_path: Path,
) -> None:
    run = _pushed_run()
    store = _save(tmp_path, run)
    merges = MergeStore()
    merged = _merge_record(MergeState.MERGED, merged_sha="c" * 40)
    merges.save(merged)
    before = _record_bytes(tmp_path)
    audit = _Audit()

    outcome = asyncio.run(
        abandon_run(
            "run-1",
            store=store,
            merge_store=merges,
            settings=_settings(tmp_path),
            reason="stop",
            audit=audit,
        )
    )

    assert outcome.exit_code == 2
    assert "already merged" in outcome.reason
    assert "delivery resume --run-id run-1" in outcome.reason
    assert merges.get(("owner/name", 9, "b" * 40)) == merged
    assert _record_bytes(tmp_path) == before
    assert audit.calls == []


def test_t6_missing_merge_store_refuses_when_a_pr_exists(
    tmp_path: Path,
) -> None:
    run = _pushed_run()
    store = _save(tmp_path, run)
    before = _record_bytes(tmp_path)

    outcome = asyncio.run(
        abandon_run(
            "run-1",
            store=store,
            merge_store=None,
            settings=_settings(tmp_path),
            reason="stop",
            audit=_Audit(),
        )
    )

    assert outcome.exit_code == 2
    assert "VECTOR_DB_DSN" in outcome.reason
    assert "CODE_REVIEW_DATABASE_URL" in outcome.reason
    assert _record_bytes(tmp_path) == before


def test_t7_live_lock_holder_refuses_with_exit_3(tmp_path: Path) -> None:
    run = _pushed_run()
    store = _save(tmp_path, run)
    lock_path = (
        tmp_path / "runs" / ".locks" / "delivery-owner-name.lock"
    )
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_at": "2026-11-27T10:00:00+00:00",
                "host": "test-host",
            }
        ),
        encoding="utf-8",
    )
    before = _record_bytes(tmp_path)

    outcome = asyncio.run(
        abandon_run(
            "run-1",
            store=store,
            merge_store=MergeStore(),
            settings=_settings(tmp_path),
            reason="stop",
            audit=_Audit(),
        )
    )

    assert outcome.exit_code == 3
    assert f"pid={os.getpid()}" in outcome.reason
    assert "started_at=2026-11-27T10:00:00+00:00" in outcome.reason
    assert "host=test-host" in outcome.reason
    assert _record_bytes(tmp_path) == before
    assert lock_path.exists()


def test_t7_stale_lock_is_replaced_with_a_warning_and_abandon_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = _pushed_run()
    store = _save(tmp_path, run)

    def fake_acquire(path: Path) -> StaleLockReplaced:
        return StaleLockReplaced(
            path,
            LockInfo(pid=os.getpid(), started_at="now", host="test-host"),
            LockInfo(
                pid=12345,
                started_at="2026-11-27T09:00:00+00:00",
                host="old-host",
            ),
        )

    monkeypatch.setattr(abandon_module, "acquire", fake_acquire)

    outcome = asyncio.run(
        abandon_run(
            "run-1",
            store=store,
            merge_store=MergeStore(),
            settings=_settings(tmp_path),
            reason="stop",
            audit=_Audit(),
        )
    )

    assert outcome.exit_code == 0
    assert store.load("run-1").state is DeliveryRunState.ABANDONED
    assert (
        "warning: replaced stale lock held by pid=12345 "
        "started_at=2026-11-27T09:00:00+00:00 host=old-host"
    ) in capsys.readouterr().err


def test_t8_success_writes_one_wal_audit_entry(tmp_path: Path) -> None:
    run = _pushed_run()
    store = _save(tmp_path, run)
    audit = _Audit()

    outcome = asyncio.run(
        abandon_run(
            "run-1",
            store=store,
            merge_store=MergeStore(),
            settings=_settings(tmp_path),
            reason="stop",
            operator="alice",
            audit=audit,
        )
    )

    assert outcome.exit_code == 0
    assert len(audit.calls) == 1
    call = audit.calls[0]
    assert call["previous"] is DeliveryRunState.WAITING_CI
    assert call["current"] is DeliveryRunState.ABANDONED
    assert call["detail"] == {
        "reason": "stop",
        "operator": "alice",
        "merge_record": "none",
    }


def test_t8_audit_failure_returns_exit_1_after_the_record_is_abandoned(
    tmp_path: Path,
) -> None:
    run = _pushed_run()
    store = _save(tmp_path, run)

    outcome = asyncio.run(
        abandon_run(
            "run-1",
            store=store,
            merge_store=MergeStore(),
            settings=_settings(tmp_path),
            reason="stop",
            audit=_Audit(error=RuntimeError("wal is down")),
        )
    )

    assert outcome.exit_code == 1
    assert outcome.audit_failed is True
    assert "run-1" in outcome.reason
    assert store.load("run-1").state is DeliveryRunState.ABANDONED


def test_r9_abandoned_is_not_promoted_by_the_daily_digest_sets() -> None:
    from apps.code_review_pipeline.delivery.digest import (
        IN_PROGRESS_DELIVERY_STATES,
        PENDING_DELIVERY_STATES,
        STUCK_DELIVERY_STATES,
    )

    assert "abandoned" not in PENDING_DELIVERY_STATES
    assert "abandoned" not in STUCK_DELIVERY_STATES
    assert "abandoned" not in IN_PROGRESS_DELIVERY_STATES
