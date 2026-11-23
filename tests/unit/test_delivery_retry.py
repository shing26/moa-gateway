from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    FailureCode,
    FailureInfo,
)
from apps.code_review_pipeline.delivery.retry import (
    RESUMABLE_FAILURE_CODES,
    RETRY_STATES,
    retry_failed_run,
)


def _request() -> DeliveryRequest:
    return DeliveryRequest(
        trace_id="trace-1",
        repo="owner/name",
        base_sha="a" * 40,
        base_branch="main",
        branch="moa/fix",
        goal="fix",
        title="fix: add",
        body="",
        source_ref="issue#7",
    )


def _run(
    state: DeliveryRunState = DeliveryRunState.FAILED,
    *,
    code: FailureCode = FailureCode.VERIFY_FAILED,
) -> DeliveryRun:
    now = datetime(2026, 11, 21, tzinfo=UTC)
    return DeliveryRun(
        schema_version=SCHEMA_VERSION,
        run_id="run-1",
        trace_id="trace-1",
        request=_request(),
        topology="single",
        state=state,
        attempts=(),
        verification=None,
        verifier_verdicts=(),
        bundle=None,
        push=None,
        merge=None,
        usage=DeliveryUsage(),
        failure=FailureInfo(code, "boom"),
        started_at=now,
        updated_at=now,
    )


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        delivery_run_record_dir=str(tmp_path / "runs"),
        delivery_repo_profile_dir=str(tmp_path / "profiles"),
        change_agent_verify_command="python -m pytest -q",
        change_agent_base_branch="main",
        change_agent_model="test-model",
        delivery_topology="single",
        delivery_max_steps=40,
        delivery_max_tokens=400_000,
        change_agent_verify_timeout_s=900.0,
    )


class _FakeStore:
    def __init__(self, run: DeliveryRun) -> None:
        self._run = run
        self.saved: list[DeliveryRun] = []

    def load(self, run_id: str) -> DeliveryRun:
        return self._run

    async def save(self, run: DeliveryRun) -> None:
        self.saved.append(run)


class _FakeFlow:
    def __init__(self, run: DeliveryRun) -> None:
        self.new_run = run
        self.calls: list[dict] = []
        self.requests: list[DeliveryRequest] = []

    async def run(self, request: DeliveryRequest) -> DeliveryRun:
        self.requests.append(request)
        return self.new_run


def _factory(flow: _FakeFlow):
    def factory(**kwargs):
        flow.calls.append(kwargs)
        return flow

    return factory


def _forbidden_factory(**kwargs):
    raise AssertionError("flow must not be built for refused retries")


def _retry_request(tmp_path: Path, store: _FakeStore, flow: _FakeFlow):
    return asyncio.run(
        retry_failed_run(
            "run-1",
            store=store,
            settings=_settings(tmp_path),
            flow_factory=_factory(flow),
        )
    )


def test_constants_cover_the_contract() -> None:
    assert RETRY_STATES == frozenset(
        {DeliveryRunState.FAILED, DeliveryRunState.BUDGET_EXHAUSTED}
    )
    assert RESUMABLE_FAILURE_CODES == frozenset(
        {
            FailureCode.NO_CHANGES,
            FailureCode.VERIFY_FAILED,
            FailureCode.VERIFIER_INVALID,
            FailureCode.VERIFIER_REVISE_EXHAUSTED,
            FailureCode.PREFLIGHT_FAILED,
            FailureCode.BUNDLE_CHANGED,
            FailureCode.INFRA_ERROR,
        }
    )


def test_retry_failed_run_creates_new_run_with_retry_provenance(
    tmp_path: Path,
) -> None:
    parent = _run()
    store = _FakeStore(parent)
    new_run = replace(
        parent,
        run_id="run-2",
        trace_id="delivery-999",
        state=DeliveryRunState.WAITING_CI,
        failure=None,
    )
    flow = _FakeFlow(new_run)

    outcome = _retry_request(tmp_path, store, flow)

    assert outcome.exit_code == 0
    assert outcome.parent_run_id == "run-1"
    assert outcome.refused is False
    assert outcome.run is new_run
    assert len(flow.requests) == 1
    request = flow.requests[0]
    assert request.repo == parent.request.repo
    assert request.branch == parent.request.branch
    assert request.goal == parent.request.goal
    assert request.title == parent.request.title
    assert request.source_ref == "retry:run-1"
    assert request.trace_id != parent.request.trace_id
    assert request.trace_id.startswith("delivery-")
    assert flow.calls[0]["store"] is store
    policy = flow.calls[0]["policy"]
    assert policy.verify_command == ("python", "-m", "pytest", "-q")
    assert policy.max_steps == 40
    assert policy.max_tokens == 400_000
    # 父记录一个字节不改：store 没有新 save，父请求保持原值。
    assert store.saved == []
    assert parent.request.trace_id == "trace-1"
    assert parent.request.source_ref == "issue#7"


def test_retry_budget_exhausted_infra_error_dispatches_to_flow(
    tmp_path: Path,
) -> None:
    parent = _run(
        state=DeliveryRunState.BUDGET_EXHAUSTED,
        code=FailureCode.INFRA_ERROR,
    )
    store = _FakeStore(parent)
    new_run = replace(
        parent,
        run_id="run-2",
        state=DeliveryRunState.READY_FOR_PUSH_APPROVAL,
        failure=None,
    )
    flow = _FakeFlow(new_run)

    outcome = _retry_request(tmp_path, store, flow)

    assert outcome.exit_code == 0
    assert flow.requests[0].source_ref == "retry:run-1"
    assert store.saved == []


@pytest.mark.parametrize(
    ("code", "reason_part"),
    [
        (FailureCode.PUSH_FAILED, "PR may already exist"),
        (FailureCode.CI_FAILED, "ci-triage"),
        (FailureCode.MERGE_FAILED, "ci-triage"),
        (FailureCode.APPROVAL_REJECTED, "bypass review"),
    ],
)
def test_retry_refuses_non_resumable_failure_codes(
    tmp_path: Path,
    code: FailureCode,
    reason_part: str,
) -> None:
    parent = _run(code=code)
    store = _FakeStore(parent)

    outcome = asyncio.run(
        retry_failed_run(
            "run-1",
            store=store,
            settings=_settings(tmp_path),
            flow_factory=_forbidden_factory,
        )
    )

    assert outcome.exit_code == 2
    assert outcome.refused is True
    assert outcome.run is None
    assert reason_part in outcome.reason
    assert store.saved == []
    assert parent.failure.code is code


def test_retry_refuses_states_outside_retry_set(tmp_path: Path) -> None:
    parent = _run(
        state=DeliveryRunState.NEEDS_HUMAN,
        code=FailureCode.INFRA_ERROR,
    )
    store = _FakeStore(parent)

    outcome = asyncio.run(
        retry_failed_run(
            "run-1",
            store=store,
            settings=_settings(tmp_path),
            flow_factory=_forbidden_factory,
        )
    )

    assert outcome.exit_code == 2
    assert outcome.refused is True
    assert "retry accepts only" in outcome.reason
    assert store.saved == []


def test_retry_refuses_approval_timed_out_record(tmp_path: Path) -> None:
    """T6：超时记录留在 ready_for_push_approval，retry 不得开新单。"""
    parent = _run(
        state=DeliveryRunState.READY_FOR_PUSH_APPROVAL,
        code=FailureCode.APPROVAL_TIMED_OUT,
    )
    store = _FakeStore(parent)

    outcome = asyncio.run(
        retry_failed_run(
            "run-1",
            store=store,
            settings=_settings(tmp_path),
            flow_factory=_forbidden_factory,
        )
    )

    assert outcome.exit_code == 2
    assert outcome.refused is True
    assert outcome.run is None
    assert "retry accepts only" in outcome.reason
    # 没有新 DeliveryRun，父记录也一个字节没改。
    assert store.saved == []
    assert parent.state is DeliveryRunState.READY_FOR_PUSH_APPROVAL


def test_retry_refuses_when_verify_command_resolves_empty(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.change_agent_verify_command = ""
    store = _FakeStore(_run())

    outcome = asyncio.run(
        retry_failed_run(
            "run-1",
            store=store,
            settings=settings,
            flow_factory=_forbidden_factory,
        )
    )

    assert outcome.exit_code == 2
    assert outcome.refused is True
    assert "CHANGE_AGENT_VERIFY_COMMAND" in outcome.reason
    assert store.saved == []


def test_retry_refuses_when_repo_profile_is_corrupt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profiles = Path(settings.delivery_repo_profile_dir)
    (profiles / "owner").mkdir(parents=True)
    (profiles / "owner" / "name.toml").write_text(
        "verify_command = [1", encoding="utf-8"
    )
    store = _FakeStore(_run())

    outcome = asyncio.run(
        retry_failed_run(
            "run-1",
            store=store,
            settings=settings,
            flow_factory=_forbidden_factory,
        )
    )

    assert outcome.exit_code == 2
    assert outcome.refused is True
    assert "仓库档案不可用" in outcome.reason
    assert store.saved == []


def test_retry_uses_the_delivery_run_lock(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    record_dir = Path(settings.delivery_run_record_dir)
    lock_path = (
        record_dir / ".locks" / "delivery-owner-name.lock"
    )
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "started_at": "t", "host": "h"}),
        encoding="utf-8",
    )
    store = _FakeStore(_run())

    outcome = asyncio.run(
        retry_failed_run(
            "run-1",
            store=store,
            settings=settings,
            flow_factory=_forbidden_factory,
        )
    )

    assert outcome.exit_code == 3
    assert outcome.refused is True
    assert outcome.run is None
    assert store.saved == []
    assert lock_path.exists()


def test_retry_maps_failed_new_run_to_exit_1(tmp_path: Path) -> None:
    parent = _run()
    store = _FakeStore(parent)
    new_run = replace(parent, run_id="run-2", state=DeliveryRunState.FAILED)
    flow = _FakeFlow(new_run)

    outcome = _retry_request(tmp_path, store, flow)

    assert outcome.exit_code == 1
    assert outcome.run is new_run


def test_retry_prints_warning_when_stale_lock_replaced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from apps.code_review_pipeline.delivery.lockfile import StaleLockReplaced, LockInfo
    from apps.code_review_pipeline.delivery import retry

    parent = _run()
    store = _FakeStore(parent)
    new_run = replace(
        parent,
        run_id="run-2",
        state=DeliveryRunState.WAITING_CI,
        failure=None,
    )
    flow = _FakeFlow(new_run)

    # Monkeypatch retry.acquire to return StaleLockReplaced
    original_acquire = retry.acquire

    def fake_acquire(path):
        holder = LockInfo(
            pid=12345,
            started_at="2026-11-21T10:00:00+00:00",
            host="test-host",
        )
        # StaleLockReplaced expects path, info (current), previous
        current = LockInfo(
            pid=os.getpid(),
            started_at="2026-11-21T10:00:01+00:00",
            host="test-host",
        )
        return StaleLockReplaced(path, current, holder)

    retry.acquire = fake_acquire
    try:
        outcome = _retry_request(tmp_path, store, flow)
    finally:
        retry.acquire = original_acquire

    # Assert the warning appears in stderr
    stderr = capsys.readouterr().err
    expected = (
        "warning: replaced stale lock held by pid=12345 "
        "started_at=2026-11-21T10:00:00+00:00 host=test-host"
    )
    assert expected in stderr

    # Assert retry still proceeds (exit_code 0 for WAITING_CI)
    assert outcome.exit_code == 0
    assert outcome.run is new_run
