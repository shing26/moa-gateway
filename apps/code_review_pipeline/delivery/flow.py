from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    FailureCode,
    FailureInfo,
)
from apps.code_review_pipeline.delivery.ports import (
    BundleChanged,
    ChangePreparerPort,
    ChangePublisher,
    DeliveryAuditPort,
    ImplementerPort,
    PreparationStatus,
    PushApprovalPort,
    PushRecoveryRequired,
    RunRecorderPort,
)
from apps.code_review_pipeline.delivery.state import assert_transition


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_run_id() -> str:
    return f"dlv-{uuid.uuid4().hex}"


class DeliveryFlow:
    def __init__(
        self,
        *,
        workspace: Path,
        policy: DeliveryPolicy,
        implementer: ImplementerPort,
        preparer: ChangePreparerPort,
        approval: PushApprovalPort,
        publisher: ChangePublisher,
        recorder: RunRecorderPort,
        audit: DeliveryAuditPort,
        clock: Callable[[], datetime] = _utcnow,
        new_run_id: Callable[[], str] = _new_run_id,
    ) -> None:
        if policy.topology != "single":
            raise ValueError(
                "verified topology requires the separately planned S1 Verifier; "
                "S0 refuses to silently degrade"
            )
        self._workspace = Path(workspace).resolve()
        self._policy = policy
        self._implementer = implementer
        self._preparer = preparer
        self._approval = approval
        self._publisher = publisher
        self._recorder = recorder
        self._audit = audit
        self._clock = clock
        self._new_run_id = new_run_id

    async def run(self, request: DeliveryRequest) -> DeliveryRun:
        now = self._clock()
        run = DeliveryRun(
            schema_version=SCHEMA_VERSION,
            run_id=self._new_run_id(),
            trace_id=request.trace_id,
            request=request,
            topology=self._policy.topology,
            state=DeliveryRunState.PREPARING,
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
        await self._recorder.save(run)
        await self._audit.record(
            run=run,
            previous=None,
            current=DeliveryRunState.PREPARING,
            detail={"action": "created"},
        )
        run = await self._move(run, DeliveryRunState.IMPLEMENTING, "start")

        feedback = ""
        revisions = 0
        while True:
            remaining_steps, remaining_tokens = self._remaining_budget(run)
            if remaining_steps <= 0 or remaining_tokens <= 0:
                return await self._finish(
                    run,
                    DeliveryRunState.BUDGET_EXHAUSTED,
                    None,
                )
            try:
                attempt = await self._implementer.run(
                    workspace=self._workspace,
                    goal=request.goal,
                    feedback=feedback,
                    max_steps=remaining_steps,
                    max_tokens=remaining_tokens,
                )
            except Exception as exc:
                target = (
                    DeliveryRunState.NEEDS_HUMAN
                    if run.state is DeliveryRunState.REVISING
                    else DeliveryRunState.FAILED
                )
                return await self._finish(
                    run,
                    target,
                    FailureInfo(
                        FailureCode.INFRA_ERROR,
                        f"implementer failed: {type(exc).__name__}: {exc}",
                    ),
                )

            run = replace(
                run,
                attempts=(*run.attempts, attempt),
                usage=run.usage + attempt.usage,
                updated_at=self._clock(),
            )
            await self._recorder.save(run)

            if attempt.status == "budget_exhausted":
                return await self._finish(
                    run,
                    DeliveryRunState.BUDGET_EXHAUSTED,
                    None,
                )
            if run.usage.total_tokens >= self._policy.max_tokens:
                return await self._finish(
                    run,
                    DeliveryRunState.BUDGET_EXHAUSTED,
                    None,
                )
            remaining_steps, remaining_tokens = self._remaining_budget(run)
            if remaining_steps <= 0 or remaining_tokens <= 0:
                return await self._finish(
                    run,
                    DeliveryRunState.BUDGET_EXHAUSTED,
                    None,
                )
            if attempt.status == "error":
                return await self._finish(
                    run,
                    DeliveryRunState.NEEDS_HUMAN
                    if run.state is DeliveryRunState.REVISING
                    else DeliveryRunState.FAILED,
                    FailureInfo(
                        FailureCode.INFRA_ERROR,
                        attempt.stop_reason or "implementer returned error",
                    ),
                )
            if attempt.status == "step_limit":
                return await self._finish(
                    run,
                    DeliveryRunState.BUDGET_EXHAUSTED,
                    None,
                )

            run = await self._move(run, DeliveryRunState.CHECKING, "verify")
            try:
                preparation = await self._preparer.prepare(
                    workspace=self._workspace,
                    request=request,
                    policy=self._policy,
                )
            except Exception as exc:
                return await self._finish(
                    run,
                    DeliveryRunState.NEEDS_HUMAN,
                    FailureInfo(
                        FailureCode.INFRA_ERROR,
                        f"preparation failed: {type(exc).__name__}: {exc}",
                    ),
                )

            if preparation.verification is not None:
                run = replace(
                    run,
                    verification=preparation.verification,
                    updated_at=self._clock(),
                )
                await self._recorder.save(run)

            if preparation.status is PreparationStatus.READY:
                if preparation.bundle is None or preparation.verification is None:
                    return await self._finish(
                        run,
                        DeliveryRunState.NEEDS_HUMAN,
                        FailureInfo(
                            FailureCode.INFRA_ERROR,
                            "ready preparation lacked bundle or verification evidence",
                        ),
                    )
                run = replace(
                    run,
                    bundle=preparation.bundle,
                    updated_at=self._clock(),
                )
                await self._recorder.save(run)
                break

            retryable = preparation.status in {
                PreparationStatus.VERIFY_FAILED,
                PreparationStatus.NO_CHANGES,
            }
            if retryable and revisions < self._policy.max_revisions:
                revisions += 1
                feedback = preparation.message or preparation.status.value
                run = await self._move(
                    run,
                    DeliveryRunState.REVISING,
                    feedback,
                )
                continue

            code = (
                FailureCode.NO_CHANGES
                if preparation.status is PreparationStatus.NO_CHANGES
                else FailureCode.VERIFY_FAILED
                if preparation.status is PreparationStatus.VERIFY_FAILED
                else FailureCode.INFRA_ERROR
            )
            return await self._finish(
                run,
                DeliveryRunState.NEEDS_HUMAN,
                FailureInfo(code, preparation.message or preparation.status.value),
            )

        assert run.bundle is not None
        run = await self._move(
            run,
            DeliveryRunState.READY_FOR_PUSH_APPROVAL,
            "awaiting_push_approval",
        )
        try:
            approval = await self._approval.request(
                run_id=run.run_id,
                bundle=run.bundle,
            )
        except Exception as exc:
            return await self._finish(
                run,
                DeliveryRunState.REJECTED,
                FailureInfo(
                    FailureCode.INFRA_ERROR,
                    f"push approval failed: {type(exc).__name__}: {exc}",
                ),
            )
        if approval is None:
            return await self._finish(
                run,
                DeliveryRunState.REJECTED,
                FailureInfo(
                    FailureCode.APPROVAL_REJECTED,
                    "human rejected or unavailable for push approval",
                ),
            )

        run = await self._move(run, DeliveryRunState.PUSHING, "approved")
        try:
            push = await self._publisher.publish(
                workspace=self._workspace,
                bundle=run.bundle,
                approval=approval,
            )
        except BundleChanged as exc:
            return await self._finish(
                run,
                DeliveryRunState.FAILED,
                FailureInfo(FailureCode.BUNDLE_CHANGED, str(exc)),
            )
        except PushRecoveryRequired as exc:
            run = replace(run, push=exc.push, updated_at=self._clock())
            await self._recorder.save(run)
            return await self._finish(
                run,
                DeliveryRunState.NEEDS_HUMAN,
                FailureInfo(FailureCode.INFRA_ERROR, str(exc)),
            )
        except Exception as exc:
            return await self._finish(
                run,
                DeliveryRunState.FAILED,
                FailureInfo(
                    FailureCode.PUSH_FAILED,
                    f"{type(exc).__name__}: {exc}",
                ),
            )
        run = replace(run, push=push, updated_at=self._clock())
        await self._recorder.save(run)
        return await self._finish(run, DeliveryRunState.WAITING_CI, None)

    async def _move(
        self,
        run: DeliveryRun,
        target: DeliveryRunState,
        detail: str,
    ) -> DeliveryRun:
        assert_transition(run.state, target)
        previous = run.state
        updated = replace(run, state=target, updated_at=self._clock())
        await self._recorder.save(updated)
        await self._audit.record(
            run=updated,
            previous=previous,
            current=target,
            detail={"action": detail},
        )
        return updated

    def _remaining_budget(self, run: DeliveryRun) -> tuple[int, int]:
        tool_steps = sum(
            1
            for attempt in run.attempts
            for step in attempt.steps
            if step.action == "call_tool"
        )
        return (
            self._policy.max_steps - tool_steps,
            self._policy.max_tokens - run.usage.total_tokens,
        )

    async def _finish(
        self,
        run: DeliveryRun,
        target: DeliveryRunState,
        failure: FailureInfo | None,
    ) -> DeliveryRun:
        updated = await self._move(run, target, failure.code.value if failure else "done")
        if failure is not None:
            updated = replace(updated, failure=failure, updated_at=self._clock())
            await self._recorder.save(updated)
            await self._audit.record(
                run=updated,
                previous=target,
                current=target,
                detail={
                    "action": "failure",
                    "code": failure.code.value,
                    "message": failure.message,
                },
            )
        return updated
