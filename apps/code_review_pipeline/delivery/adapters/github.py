from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.pipeline import (
    ChangeApproval,
    ChangeBundleChanged,
    ChangePipeline,
    ChangeRequest,
    ChangeStatus,
)
from apps.code_review_pipeline.delivery.models import (
    DeliveryPolicy,
    DeliveryRequest,
    PushOutcome,
    VerificationEvidence,
)
from apps.code_review_pipeline.delivery.ports import (
    BundleChanged,
    ChangePreparation,
    PreparationStatus,
    PushRecoveryRequired,
)
from apps.code_review_pipeline.merge_state import initial_transition
from apps.code_review_pipeline.merge_store import MergeRecord


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GitHubChangeAdapter:
    def __init__(
        self,
        *,
        pipeline: ChangePipeline,
        merge_store: Any,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._pipeline = pipeline
        self._merge_store = merge_store
        self._clock = clock

    def _require_workspace(self, workspace: Path) -> Path:
        root = Path(workspace).resolve()
        if root != self._pipeline.workspace.resolve():
            raise ValueError(
                f"workspace differs from pipeline workspace: {root} "
                f"!= {self._pipeline.workspace.resolve()}"
            )
        return root

    @property
    def repo(self) -> str:
        return self._pipeline.repo

    @property
    def verify_command(self) -> tuple[str, ...]:
        return self._pipeline.verify_command

    async def prepare(
        self,
        *,
        workspace: Path,
        request: DeliveryRequest,
        policy: DeliveryPolicy,
    ) -> ChangePreparation:
        self._require_workspace(workspace)
        if tuple(policy.verify_command) != self._pipeline.verify_command:
            raise ValueError(
                "delivery policy verify_command differs from change pipeline"
            )
        result = await self._pipeline.prepare_bundle(
            ChangeRequest(
                repo=request.repo,
                base_sha=request.base_sha,
                title=request.title,
                body=request.body,
                goal=request.goal,
                branch=request.branch,
                trace_id=request.trace_id,
            )
        )
        if result.status is ChangeStatus.VERIFIED:
            assert result.bundle is not None
            output = result.bundle.verify_output
            verification = VerificationEvidence(
                command=result.bundle.verify_command,
                exit_code=result.bundle.verify_exit_code,
                output_hash=(
                    result.bundle.verify_output_hash
                    or hashlib.sha256(output.encode("utf-8")).hexdigest()
                ),
                output_excerpt=output[-4000:],
            )
            return ChangePreparation(
                status=PreparationStatus.READY,
                bundle=result.bundle,
                verification=verification,
                message=result.message,
            )
        if result.status is ChangeStatus.VERIFY_FAILED:
            assert result.bundle is not None
            output = result.bundle.verify_output
            return ChangePreparation(
                status=PreparationStatus.VERIFY_FAILED,
                bundle=result.bundle,
                verification=VerificationEvidence(
                    command=result.bundle.verify_command,
                    exit_code=result.bundle.verify_exit_code,
                    output_hash=(
                        result.bundle.verify_output_hash
                        or hashlib.sha256(output.encode("utf-8")).hexdigest()
                    ),
                    output_excerpt=output[-4000:],
                ),
                message=result.message,
            )
        if result.status is ChangeStatus.NO_CHANGES:
            return ChangePreparation(
                status=PreparationStatus.NO_CHANGES,
                bundle=result.bundle,
                verification=None,
                message=result.message,
            )
        return ChangePreparation(
            status=PreparationStatus.INVALID,
            bundle=result.bundle,
            verification=None,
            message=result.message or result.status.value,
        )

    async def publish(
        self,
        *,
        workspace: Path,
        bundle: ChangeBundle,
        approval: ChangeApproval,
    ) -> PushOutcome:
        self._require_workspace(workspace)
        try:
            result = await self._pipeline.publish_bundle(bundle, approval)
        except ChangeBundleChanged as exc:
            raise BundleChanged(str(exc)) from exc
        if result.status is not ChangeStatus.PUSHED:
            raise RuntimeError(
                f"change pipeline refused publish: {result.status.value}: "
                f"{result.message}"
            )
        if result.pull_request is None or not result.commit_sha:
            raise RuntimeError("pushed change did not return PR identity and commit SHA")
        push = PushOutcome(
            branch=bundle.branch,
            commit_sha=result.commit_sha,
            pull_request=result.pull_request,
        )
        try:
            self._register_merge_record(
                push=push,
                run_id=approval.run_id,
                trace_id=bundle.trace_id,
                bundle_hash=bundle.bundle_hash,
            )
        except Exception as exc:
            raise PushRecoveryRequired(
                push,
                "PR was created but merge registration failed: "
                f"{type(exc).__name__}: {exc}",
            ) from exc
        return push

    def recover(
        self,
        *,
        push: PushOutcome,
        run_id: str,
        trace_id: str,
        bundle_hash: str,
    ) -> MergeRecord:
        """Re-register a known successful push without repeating remote writes."""
        return self._register_merge_record(
            push=push,
            run_id=run_id,
            trace_id=trace_id,
            bundle_hash=bundle_hash,
        )

    def _register_merge_record(
        self,
        *,
        push: PushOutcome,
        run_id: str,
        trace_id: str,
        bundle_hash: str,
    ) -> MergeRecord:
        identity = (
            self._pipeline.repo,
            push.pull_request.number,
            push.pull_request.head_sha,
        )
        now = self._clock().isoformat()
        record = self._merge_store.get(identity)
        if record is None:
            record = MergeRecord(
                repo=self._pipeline.repo,
                pr_number=push.pull_request.number,
                head_sha=push.pull_request.head_sha,
                run_id=run_id,
                trace_id=trace_id,
                bundle_hash=bundle_hash,
                transitions=tuple(
                    initial_transition(
                        now,
                        run_id=run_id,
                        trace_id=trace_id,
                        bundle_hash=bundle_hash,
                    )
                ),
            )
            self._merge_store.save(record)
        self._merge_store.supersede_older(
            record.repo,
            record.pr_number,
            record.head_sha,
            at=now,
        )
        return self._merge_store.get(identity) or record
