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
    PushApprovalTimedOut,
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
        self.details: list[dict[str, object]] = []

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
        self.details.append(detail)


def _flow(
    tmp_path: Path,
    *,
    implementer: FakeImplementer,
    preparer: FakePreparer,
    approval: FakeApproval | None = None,
    publisher: FakePublisher | None = None,
    max_steps: int = 40,
    github: object | None = None,
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
            run_id="run-1",
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
        github=github,
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
    # 验证没有 github 时，preparing -> implementing 的 detail 只有 action 键
    detail = audit.details[1]
    assert list(detail.keys()) == ["action"]


@pytest.mark.asyncio
async def test_approved_audit_detail_carries_identity_fields(tmp_path: Path) -> None:
    """推送审批通过的审计行必须带五项身份字段（A2 判据 5）。

    哪怕 identity_verified=False（终端自称的名字，未验证），也比查不到强。
    """
    bundle = _bundle()
    approval = ChangeApproval(
        approver="alice",
        trace_id="trace-1",
        bundle_hash=bundle.bundle_hash,
        run_id="run-1",
        channel="terminal",
        identity_verified=False,
        operator_login="bob",
        gate="allowlist",
    )
    flow, _, audit, publisher = _flow(
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
        approval=FakeApproval(approval),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.WAITING_CI
    assert publisher.calls == 1
    idx = audit.transitions.index(("ready_for_push_approval", "pushing"))
    detail = audit.details[idx]
    assert detail["action"] == "approved"
    assert detail["approver"] == "alice"
    assert detail["channel"] == "terminal"
    assert detail["identity_verified"] is False
    assert detail["operator_login"] == "bob"
    assert detail["gate"] == "allowlist"


class _FakePreflightGithub:
    """暴露 run_preflight 用到的三个只读方法的假 GitHub。"""

    def __init__(self, *, permission: str, push: bool) -> None:
        self._permission = permission
        self._push = push

    async def get_authenticated_login(self) -> str:
        return "alice"

    async def get_repo(self, repo):
        return {"permission": self._permission, "permissions": {"push": self._push}}

    async def get_branch_sha(self, repo, branch: str) -> str:
        return "a" * 40


@pytest.mark.asyncio
async def test_blocked_preflight_terminates_before_any_implementer_attempt(
    tmp_path: Path,
) -> None:
    """只读 token 在实现开始前就终止——一个实现步都不花（issue #59 第一半）。"""
    implementer = FakeImplementer([_attempt()])
    flow, recorder, audit, _ = _flow(
        tmp_path,
        implementer=implementer,
        preparer=FakePreparer([]),
        github=_FakePreflightGithub(permission="read", push=False),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure is not None
    assert run.failure.code is FailureCode.PREFLIGHT_FAILED
    assert "push" in run.failure.message
    assert "permission=read" in run.failure.message
    assert implementer.feedback == []  # 没有进入实现
    assert ("preparing", "needs_human") in audit.transitions
    assert ("preparing", "implementing") not in audit.transitions
    # 验证 audit detail 包含 preflight 报告且 push 状态为 blocked
    detail = audit.details[audit.transitions.index(("preparing", "needs_human"))]
    assert "preflight" in detail
    assert any(c["name"] == "push" and c["status"] == "blocked" for c in detail["preflight"]["checks"])
    assert recorder.runs[-1] == run


@pytest.mark.asyncio
async def test_passing_preflight_leaves_the_flow_unchanged(tmp_path: Path) -> None:
    bundle = _bundle()
    flow, _, audit, publisher = _flow(
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
        github=_FakePreflightGithub(permission="admin", push=True),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.WAITING_CI
    assert run.failure is None
    assert publisher.calls == 1
    # 开头多不出新迁移：preflight 通过不产生额外状态跳转。
    assert audit.transitions[:2] == [("", "preparing"), ("preparing", "implementing")]
    # 验证 preparing -> implementing 的 detail 包含 preflight 报告
    detail = audit.details[1]
    assert "preflight" in detail
    assert "action" in detail and detail["action"] == "start"


@pytest.mark.asyncio
async def test_ready_for_push_approval_detail_carries_message_when_nonempty(
    tmp_path: Path,
) -> None:
    """READY_FOR_PUSH_APPROVAL 的 audit detail 在 preparation.message 非空时
    必须带 message 键（issue #70：verify 覆盖缺口要在准备期可见）。"""
    bundle = _bundle()
    preparer = FakePreparer(
        [
            ChangePreparation(
                status=PreparationStatus.READY,
                bundle=bundle,
                verification=_verification(),
                message="verification passed; uncovered files: tests/test_bench.py",
            )
        ]
    )
    flow, _, audit, _ = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt()]),
        preparer=preparer,
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.WAITING_CI
    idx = audit.transitions.index(("checking", "ready_for_push_approval"))
    detail = audit.details[idx]
    assert detail.get("action") == "awaiting_push_approval"
    assert detail.get("message") == (
        "verification passed; uncovered files: tests/test_bench.py"
    )


@pytest.mark.asyncio
async def test_ready_for_push_approval_detail_has_only_action_when_message_empty(
    tmp_path: Path,
) -> None:
    """preparation.message 为空时，detail 只有 action 键，与历史行为一致。"""
    bundle = _bundle()
    preparer = FakePreparer(
        [
            ChangePreparation(
                status=PreparationStatus.READY,
                bundle=bundle,
                verification=_verification(),
                message="",
            )
        ]
    )
    flow, _, audit, _ = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt()]),
        preparer=preparer,
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.WAITING_CI
    idx = audit.transitions.index(("checking", "ready_for_push_approval"))
    detail = audit.details[idx]
    assert list(detail.keys()) == ["action"]
    assert detail["action"] == "awaiting_push_approval"


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
    assert implementer.feedback[0] == ""
    # 修订轮不能只收到"失败了"：命令、退出码与输出尾部必须一起回流。
    assert implementer.feedback[1].startswith("tests failed")
    assert "Exit code: 1" in implementer.feedback[1]
    assert "1 passed" in implementer.feedback[1]
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
async def test_gate_failure_uses_one_revision_and_passes_feedback(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    implementer = FakeImplementer([_attempt(), _attempt(summary="removed import")])
    preparer = FakePreparer(
        [
            ChangePreparation(
                status=PreparationStatus.GATE_FAILED,
                bundle=bundle,
                verification=None,
                message="门禁未通过: tests/unit/x.py:F401 unused import",
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
    assert implementer.feedback[0] == ""
    assert implementer.feedback[1].startswith("门禁未通过")
    assert "F401" in implementer.feedback[1]
    assert preparer.calls == 2


@pytest.mark.asyncio
async def test_repeated_gate_failure_stops_as_verify_failed(
    tmp_path: Path,
) -> None:
    gate_failed = ChangePreparation(
        status=PreparationStatus.GATE_FAILED,
        bundle=_bundle(),
        verification=None,
        message="门禁未通过: tests/unit/x.py:F401 unused import",
    )
    implementer = FakeImplementer([_attempt(), _attempt(summary="still broken")])
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=implementer,
        preparer=FakePreparer([gate_failed, gate_failed]),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure == FailureInfo(
        FailureCode.VERIFY_FAILED,
        "门禁未通过: tests/unit/x.py:F401 unused import",
    )
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
async def test_approval_transport_failure_needs_human_retry(tmp_path: Path) -> None:
    class UnavailableApproval(FakeApproval):
        async def request(self, *, run_id: str, bundle: ChangeBundle):
            raise RuntimeError("approval service unavailable")

    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt()]),
        preparer=FakePreparer(
            [
                ChangePreparation(
                    status=PreparationStatus.READY,
                    bundle=_bundle(),
                    verification=_verification(),
                )
            ]
        ),
        approval=UnavailableApproval(None),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure is not None
    assert run.failure.code is FailureCode.INFRA_ERROR
    assert "approval service unavailable" in run.failure.message
    assert publisher.calls == 0


@pytest.mark.asyncio
async def test_push_approval_timeout_stays_ready_for_push_approval(
    tmp_path: Path,
) -> None:
    """T2：超时 ≠ 拒绝；记录原地记 approval_timed_out，不进 REJECTED。"""

    class TimedOutApproval(FakeApproval):
        async def request(self, *, run_id: str, bundle: ChangeBundle):
            raise PushApprovalTimedOut("approval window expired")

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
        approval=TimedOutApproval(None),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.READY_FOR_PUSH_APPROVAL
    assert run.failure is not None
    assert run.failure.code is FailureCode.APPROVAL_TIMED_OUT
    assert run.bundle == bundle
    assert publisher.calls == 0
    assert ("ready_for_push_approval", "rejected") not in audit.transitions
    idx = audit.transitions.index(
        ("ready_for_push_approval", "ready_for_push_approval")
    )
    detail = audit.details[idx]
    assert detail["action"] == "failure"
    assert detail["code"] == FailureCode.APPROVAL_TIMED_OUT.value
    assert recorder.runs[-1] == run


@pytest.mark.parametrize("returned_run_id", ["", "run-other"])
@pytest.mark.asyncio
async def test_approval_for_a_different_run_blocks_publish(
    tmp_path: Path,
    returned_run_id: str,
) -> None:
    bundle = _bundle()
    approval = FakeApproval(
        ChangeApproval(
            approver="alice",
            trace_id="trace-1",
            bundle_hash=bundle.bundle_hash,
            run_id=returned_run_id,
        )
    )
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

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure is not None
    assert run.failure.code is FailureCode.INFRA_ERROR
    assert "run_id" in run.failure.message
    assert publisher.calls == 0


@pytest.mark.parametrize(
    ("trace_id", "bundle_hash"),
    [
        ("other-trace", _bundle().bundle_hash),
        ("trace-1", "0" * 64),
    ],
)
@pytest.mark.asyncio
async def test_approval_for_a_different_bundle_never_enters_pushing(
    tmp_path: Path,
    trace_id: str,
    bundle_hash: str,
) -> None:
    bundle = _bundle()
    flow, _, audit, publisher = _flow(
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
        approval=FakeApproval(
            ChangeApproval(
                approver="alice",
                trace_id=trace_id,
                bundle_hash=bundle_hash,
                run_id="run-1",
            )
        ),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure is not None
    assert run.failure.code is FailureCode.APPROVAL_REJECTED
    assert "pushing" not in [current for _, current in audit.transitions]
    assert publisher.calls == 0


@pytest.mark.asyncio
async def test_no_changes_stops_without_an_extra_implementer_attempt(
    tmp_path: Path,
) -> None:
    implementer = FakeImplementer([_attempt(summary="made no edits")])
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=implementer,
        preparer=FakePreparer(
            [
                ChangePreparation(
                    status=PreparationStatus.NO_CHANGES,
                    bundle=None,
                    verification=_verification(),
                    message="no files changed",
                )
            ]
        ),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure is not None
    assert run.failure.code is FailureCode.NO_CHANGES
    assert len(run.attempts) == 1
    assert publisher.calls == 0


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
    assert implementer.feedback[0] == ""
    assert implementer.feedback[1].startswith("tests failed")
    assert "Exit code: 1" in implementer.feedback[1]
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


# ── A1 flow 级绑定（修正案 1 P0 回归） ──────────────────────────────────


class _SharedHitlStorage:
    """Redis 替身：``using_memory=False`` 表示跨进程可用（内存字典实现）。"""

    using_memory = False

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def key(self, hitl_id: str) -> str:
        return f"moa:hitl:{hitl_id}"

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self.data[key] = value

    def pop(self, key: str) -> str | None:
        return self.data.pop(key, None)

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def clear(self) -> None:
        self.data.clear()


class _PushApprovalCardSink:
    def __init__(self) -> None:
        self.cards: list[object] = []

    async def send_card(self, card: object) -> bool:
        self.cards.append(card)
        return True


class _PortGithub:
    async def get_authenticated_login(self) -> str:
        return "alice"


class _PortRunLookup:
    def load(self, run_id: str) -> object:
        from types import SimpleNamespace

        if run_id != "run-1":
            raise FileNotFoundError(run_id)
        return SimpleNamespace(request=SimpleNamespace(repo="owner/name"))


class _HitlBoundApproval:
    """复现修正案 1 之前的缺陷：批准对象把 ``hitl_id`` 当成 ``trace_id``。"""

    def __init__(self, approval: ChangeApproval) -> None:
        self._approval = approval

    async def request(self, *, run_id: str, bundle: ChangeBundle) -> ChangeApproval:
        assert run_id == "run-1"
        assert bundle.trace_id == "trace-1"
        return self._approval


class _RecordingApprovalPort:
    """转发给真实端口，同时记录它返回的批准对象（A9 断言用）。"""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.approvals: list[object] = []

    async def request(self, *, run_id: str, bundle: ChangeBundle) -> object:
        approval = await self._inner.request(run_id=run_id, bundle=bundle)  # type: ignore[attr-defined]
        self.approvals.append(approval)
        return approval


@pytest.mark.asyncio
async def test_feishu_port_approval_binds_to_bundle_trace_id_and_pushes(
    tmp_path: Path,
) -> None:
    """flow 级 A9：真实飞书端口返回的批准对象必须能进 PUSHING 并触发发布。"""
    import asyncio
    from types import SimpleNamespace

    from app.engine import SessionStore
    from apps.code_review_pipeline.delivery.feishu_approval import (
        FeishuPushApprovalPort,
        write_decision,
    )

    bundle = _bundle()
    store = SessionStore(storage=_SharedHitlStorage())
    sender = _PushApprovalCardSink()
    port = FeishuPushApprovalPort(
        card_sender=sender,
        session_store=store,
        run_store=_PortRunLookup(),
        github=_PortGithub(),
        settings_obj=SimpleNamespace(
            feishu_push_approval_enabled=True,
            feishu_push_approval_timeout_s=5,
            feishu_operator_open_id="ou_operator",
            delivery_approval_chat_id="oc_chat",
            feishu_app_id="cli_test",
            feishu_app_secret="secret",
            hitl_approver_ids=("ou_approver",),
            gateway_allow_insecure="0",
        ),
        id_factory=lambda: "hitl-1",
        poll_interval_s=0.01,
    )
    recorder = _RecordingApprovalPort(port)
    flow, _, audit, publisher = _flow(
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
        approval=recorder,
    )

    task = asyncio.create_task(flow.run(_request()))
    for _ in range(500):
        if sender.cards:
            break
        await asyncio.sleep(0)
    assert sender.cards, "飞书审批卡未发出，flow 没有停在 READY_FOR_PUSH_APPROVAL"
    # 卡片按钮回传的 trace_id 必须是 pending 记录的 key（hitl_id），
    # 否则回调 pop_hitl 认领不到，远程批准永远落空。
    assert sender.cards[0].trace_id == "hitl-1"
    assert write_decision(
        store,
        "hitl-1",
        {
            "decision": "approve",
            "approver": "ou_approver",
            "channel": "feishu",
            "identity_verified": True,
            "gate": "allowlist",
            "bundle_hash": bundle.bundle_hash,
        },
        ttl_s=60,
    )
    run = await task

    assert recorder.approvals, "飞书端口没有返回批准对象"
    approval = recorder.approvals[-1]
    assert approval is not None
    assert approval.trace_id == bundle.trace_id, (
        f"trace_id={approval.trace_id!r} != {bundle.trace_id!r}"
    )
    assert approval.bundle_hash == bundle.bundle_hash, (
        f"bundle_hash={approval.bundle_hash!r} != {bundle.bundle_hash!r}"
    )
    assert run.state is DeliveryRunState.WAITING_CI, run.failure
    assert ("ready_for_push_approval", "pushing") in audit.transitions
    assert publisher.calls == 1
    idx = audit.transitions.index(("ready_for_push_approval", "pushing"))
    assert audit.details[idx]["channel"] == "feishu"
    assert audit.details[idx]["approver"] == "ou_approver"


@pytest.mark.asyncio
async def test_flow_refuses_approval_bound_to_hitl_id(tmp_path: Path) -> None:
    """``trace_id=hitl_id`` 是修正案 1 修掉的 P0；它必须停在 PUSHING 之前。"""
    bundle = _bundle()
    flow, _, audit, publisher = _flow(
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
        approval=_HitlBoundApproval(
            ChangeApproval(
                approver="ou_approver",
                trace_id="hitl-1",
                bundle_hash=bundle.bundle_hash,
                run_id="run-1",
                channel="feishu",
            )
        ),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure is not None
    assert run.failure.code is FailureCode.APPROVAL_REJECTED
    assert "pushing" not in [current for _, current in audit.transitions]
    assert publisher.calls == 0
