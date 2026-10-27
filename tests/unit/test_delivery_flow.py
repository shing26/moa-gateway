from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.pipeline import ChangeApproval
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.delivery.flow import DeliveryFlow
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    AgentStep,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    FailureCode,
    FailureInfo,
    PushOutcome,
    VerificationEvidence,
)
from apps.code_review_pipeline.delivery.ports import (
    BundleChanged,
    ChangePreparation,
    PreparationStatus,
    PushRecoveryRequired,
)


def _request() -> DeliveryRequest:
    return DeliveryRequest(
        trace_id="trace-1",
        repo="owner/name",
        base_sha="a" * 40,
        base_branch="main",
        branch="moa/fix-1",
        goal="fix add",
        title="fix: correct add",
        body="body",
    )


def _attempt(
    *,
    status: str = "completed",
    summary: str = "changed app.py",
    tool_calls: int = 1,
) -> AgentAttempt:
    return AgentAttempt(
        role="implementer",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        stop_reason="finished",
        usage=DeliveryUsage(
            llm_calls=1,
            prompt_tokens=100,
            completion_tokens=20,
            cost_usd=0.001,
            llm_latency_ms=10.0,
        ),
        steps=tuple(
            AgentStep(
                index=index,
                action="call_tool",
                tool="write_file",
                arguments={"path": "app.py"},
                observation="ok",
                outcome="ok",
                elapsed_ms=2.0,
            )
            for index in range(1, tool_calls + 1)
        ),
    )


def _bundle() -> ChangeBundle:
    patch = "diff --git a/app.py b/app.py\n"
    return ChangeBundle(
        base_sha="a" * 40,
        branch="moa/fix-1-trace-1",
        title="fix: correct add",
        body="body",
        files=(),
        patch=patch,
        patch_hash=hashlib.sha256(patch.encode()).hexdigest(),
        verify_command=("python", "-m", "pytest", "-q"),
        verify_exit_code=0,
        verify_output="1 passed",
        model="test-model",
        trace_id="trace-1",
    )


def _verification() -> VerificationEvidence:
    return VerificationEvidence(
        command=("python", "-m", "pytest", "-q"),
        exit_code=0,
        output_hash=hashlib.sha256(b"1 passed").hexdigest(),
        output_excerpt="1 passed",
    )


class FakeImplementer:
    def __init__(self, attempts: list[AgentAttempt]) -> None:
        self._attempts = list(attempts)
        self.feedback: list[str] = []

    async def run(
        self,
        *,
        workspace: Path,
        goal: str,
        feedback: str,
        max_steps: int,
        max_tokens: int,
    ) -> AgentAttempt:
        self.feedback.append(feedback)
        return self._attempts.pop(0)


class FakePreparer:
    def __init__(self, results: list[ChangePreparation]) -> None:
        self._results = list(results)
        self.calls = 0

    async def prepare(
        self,
        *,
        workspace: Path,
        request: DeliveryRequest,
        policy: DeliveryPolicy,
    ) -> ChangePreparation:
        self.calls += 1
        return self._results.pop(0)


class FakeApproval:
    def __init__(self, approval: ChangeApproval | None) -> None:
        self._approval = approval
        self.bundles: list[ChangeBundle] = []

    async def request(
        self,
        *,
        run_id: str,
        bundle: ChangeBundle,
    ) -> ChangeApproval | None:
        self.bundles.append(bundle)
        return self._approval


class FakePublisher:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.calls = 0

    async def publish(
        self,
        *,
        workspace: Path,
        bundle: ChangeBundle,
        approval: ChangeApproval,
    ) -> PushOutcome:
        self.calls += 1
        self._events.append("publish")
        return PushOutcome(
            branch=bundle.branch,
            commit_sha="b" * 40,
            pull_request=PullRequestRef(
                number=9,
                url="https://example.com/pr/9",
                head_sha="b" * 40,
            ),
        )


class FakeRecorder:
    def __init__(self) -> None:
        self.runs: list[DeliveryRun] = []

    async def save(self, run: DeliveryRun) -> None:
        self.runs.append(run)


class FakeAudit:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.transitions: list[tuple[str, str]] = []

    async def record(
        self,
        *,
        run: DeliveryRun,
        previous: DeliveryRunState | None,
        current: DeliveryRunState,
        detail: dict[str, object],
    ) -> None:
        self._events.append(f"audit:{current.value}")
        self.transitions.append(
            (
                previous.value if previous else "",
                current.value,
            )
        )


def _flow(
    tmp_path: Path,
    *,
    implementer: FakeImplementer,
    preparer: FakePreparer,
    approval: FakeApproval | None = None,
    publisher: FakePublisher | None = None,
    max_steps: int = 40,
) -> tuple[DeliveryFlow, FakeRecorder, FakeAudit, FakePublisher]:
    events: list[str] = []
    recorder = FakeRecorder()
    audit = FakeAudit(events)
    publisher = publisher or FakePublisher(events)
    approval = approval or FakeApproval(
        ChangeApproval(
            approver="alice",
            trace_id="trace-1",
            bundle_hash=_bundle().bundle_hash,
        )
    )
    flow = DeliveryFlow(
        workspace=tmp_path,
        policy=DeliveryPolicy(
            verify_command=("python", "-m", "pytest", "-q"),
            max_steps=max_steps,
        ),
        implementer=implementer,
        preparer=preparer,
        approval=approval,
        publisher=publisher,
        recorder=recorder,
        audit=audit,
        clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
        new_run_id=lambda: "run-1",
    )
    return flow, recorder, audit, publisher


@pytest.mark.asyncio
async def test_s0_happy_path_reaches_waiting_ci(tmp_path: Path) -> None:
    bundle = _bundle()
    flow, recorder, audit, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt()]),
        preparer=FakePreparer(
            [
                ChangePreparation(
                    status=PreparationStatus.READY,
                    bundle=bundle,
                    verification=_verification(),
                )
            ]
        ),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.WAITING_CI
    assert run.bundle == bundle
    assert run.push is not None
    assert run.push.pull_request.number == 9
    assert run.usage == _attempt().usage
    assert publisher.calls == 1
    assert run.failure is None
    assert run.run_id == "run-1"
    assert recorder.runs[-1] == run
    assert audit.transitions == [
        ("", "preparing"),
        ("preparing", "implementing"),
        ("implementing", "checking"),
        ("checking", "ready_for_push_approval"),
        ("ready_for_push_approval", "pushing"),
        ("pushing", "waiting_ci"),
    ]


@pytest.mark.asyncio
async def test_verify_failure_uses_one_revision_and_passes_feedback(tmp_path: Path) -> None:
    bundle = _bundle()
    implementer = FakeImplementer([_attempt(), _attempt(summary="fixed second time")])
    preparer = FakePreparer(
        [
            ChangePreparation(
                status=PreparationStatus.VERIFY_FAILED,
                bundle=bundle,
                verification=replace(_verification(), exit_code=1),
                message="tests failed",
            ),
            ChangePreparation(
                status=PreparationStatus.READY,
                bundle=bundle,
                verification=_verification(),
            ),
        ]
    )
    flow, _, _, _ = _flow(tmp_path, implementer=implementer, preparer=preparer)

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.WAITING_CI
    assert len(run.attempts) == 2
    assert implementer.feedback == ["", "tests failed"]
    assert preparer.calls == 2


@pytest.mark.asyncio
async def test_second_verify_failure_stops_without_a_third_implementer(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    implementer = FakeImplementer([_attempt(), _attempt(summary="still broken")])
    failed = ChangePreparation(
        status=PreparationStatus.VERIFY_FAILED,
        bundle=bundle,
        verification=replace(_verification(), exit_code=1),
        message="tests failed",
    )
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=implementer,
        preparer=FakePreparer([failed, failed]),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure == FailureInfo(FailureCode.VERIFY_FAILED, "tests failed")
    assert len(run.attempts) == 2
    assert publisher.calls == 0


@pytest.mark.asyncio
async def test_human_rejection_stops_before_publish(tmp_path: Path) -> None:
    bundle = _bundle()
    approval = FakeApproval(None)
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt()]),
        preparer=FakePreparer(
            [
                ChangePreparation(
                    status=PreparationStatus.READY,
                    bundle=bundle,
                    verification=_verification(),
                )
            ]
        ),
        approval=approval,
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.REJECTED
    assert run.failure is not None
    assert run.failure.code is FailureCode.APPROVAL_REJECTED
    assert publisher.calls == 0
    assert approval.bundles == [bundle]


@pytest.mark.asyncio
async def test_budget_exhaustion_is_not_a_verification_failure(tmp_path: Path) -> None:
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt(status="budget_exhausted")]),
        preparer=FakePreparer([]),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.BUDGET_EXHAUSTED
    assert run.failure is None
    assert publisher.calls == 0


@pytest.mark.asyncio
async def test_initial_step_limit_is_budget_exhaustion(tmp_path: Path) -> None:
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt(status="step_limit")]),
        preparer=FakePreparer([]),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.BUDGET_EXHAUSTED
    assert run.failure is None
    assert publisher.calls == 0


@pytest.mark.asyncio
async def test_revision_step_limit_is_budget_exhaustion(tmp_path: Path) -> None:
    failed = ChangePreparation(
        status=PreparationStatus.VERIFY_FAILED,
        bundle=_bundle(),
        verification=replace(_verification(), exit_code=1),
        message="tests failed",
    )
    implementer = FakeImplementer(
        [
            _attempt(),
            _attempt(status="step_limit", summary="still working"),
        ]
    )
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=implementer,
        preparer=FakePreparer([failed]),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.BUDGET_EXHAUSTED
    assert run.failure is None
    assert len(run.attempts) == 2
    assert implementer.feedback == ["", "tests failed"]
    assert publisher.calls == 0


@pytest.mark.asyncio
async def test_run_total_step_budget_is_rechecked_after_attempt(
    tmp_path: Path,
) -> None:
    preparer = FakePreparer([])
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt(tool_calls=1)]),
        preparer=preparer,
        max_steps=1,
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.BUDGET_EXHAUSTED
    assert run.failure is None
    assert preparer.calls == 0
    assert publisher.calls == 0


@pytest.mark.asyncio
async def test_s0_rejects_verified_topology_without_silent_degrade(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="verified topology requires"):
        DeliveryFlow(
            workspace=tmp_path,
            policy=DeliveryPolicy(
                verify_command=("pytest",),
                topology="verified",
            ),
            implementer=FakeImplementer([]),
            preparer=FakePreparer([]),
            approval=FakeApproval(None),
            publisher=FakePublisher([]),
            recorder=FakeRecorder(),
            audit=FakeAudit([]),
            clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
            new_run_id=lambda: "run-1",
        )


@pytest.mark.asyncio
async def test_workspace_change_after_approval_maps_to_bundle_changed(
    tmp_path: Path,
) -> None:
    class ChangedPublisher(FakePublisher):
        async def publish(
            self,
            *,
            workspace: Path,
            bundle: ChangeBundle,
            approval: ChangeApproval,
        ) -> PushOutcome:
            raise BundleChanged("file changed after bundle: app.py")

    bundle = _bundle()
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt()]),
        preparer=FakePreparer(
            [
                ChangePreparation(
                    status=PreparationStatus.READY,
                    bundle=bundle,
                    verification=_verification(),
                )
            ]
        ),
        publisher=ChangedPublisher([]),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.FAILED
    assert run.failure == FailureInfo(
        FailureCode.BUNDLE_CHANGED,
        "file changed after bundle: app.py",
    )
    assert publisher.calls == 0


@pytest.mark.asyncio
async def test_merge_registration_failure_preserves_push_for_recovery(
    tmp_path: Path,
) -> None:
    push = PushOutcome(
        branch="moa/fix-1-trace-1",
        commit_sha="b" * 40,
        pull_request=PullRequestRef(
            number=9,
            url="https://example.com/pr/9",
            head_sha="b" * 40,
        ),
    )

    class RecoverablePublisher(FakePublisher):
        async def publish(
            self,
            *,
            workspace: Path,
            bundle: ChangeBundle,
            approval: ChangeApproval,
        ) -> PushOutcome:
            self.calls += 1
            raise PushRecoveryRequired(
                push,
                "PR created but merge registration failed",
            )

    bundle = _bundle()
    flow, recorder, _, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt()]),
        preparer=FakePreparer(
            [
                ChangePreparation(
                    status=PreparationStatus.READY,
                    bundle=bundle,
                    verification=_verification(),
                )
            ]
        ),
        publisher=RecoverablePublisher([]),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure is not None
    assert run.failure.code is FailureCode.INFRA_ERROR
    assert run.push == push
    assert recorder.runs[-1].push == push
    assert publisher.calls == 1
