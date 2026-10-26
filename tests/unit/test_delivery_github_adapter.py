from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.pipeline import (
    ChangeApproval,
    ChangeResult,
    ChangeStatus,
)
from apps.code_review_pipeline.delivery.adapters.github import GitHubChangeAdapter
from apps.code_review_pipeline.delivery.models import DeliveryPolicy, DeliveryRequest
from apps.code_review_pipeline.delivery.ports import PreparationStatus
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeStore
from apps.code_review_pipeline.change.refs import PullRequestRef


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
        files=(),
        patch="",
        patch_hash="p" * 64,
        verify_command=("pytest",),
        verify_exit_code=0,
        verify_output="1 passed",
        model="test-model",
        trace_id="trace-1",
    )


class FakePipeline:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self.prepare_calls = 0
        self.publish_calls = 0

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def repo(self) -> str:
        return "owner/name"

    @property
    def verify_command(self) -> tuple[str, ...]:
        return ("pytest",)

    async def prepare_bundle(self, request):
        self.prepare_calls += 1
        return ChangeResult(
            status=ChangeStatus.VERIFIED,
            bundle=_bundle(),
            message="ready",
        )

    async def publish_bundle(self, bundle, approval):
        self.publish_calls += 1
        return ChangeResult(
            status=ChangeStatus.PUSHED,
            bundle=bundle,
            message="pushed",
            pr_url="https://example.com/pr/5",
            commit_sha="b" * 40,
            pull_request=PullRequestRef(
                number=5,
                url="https://example.com/pr/5",
                head_sha="b" * 40,
            ),
        )


@pytest.mark.asyncio
async def test_preparer_maps_verified_change_into_port_result(tmp_path: Path) -> None:
    pipeline = FakePipeline(tmp_path)
    adapter = GitHubChangeAdapter(pipeline=pipeline, merge_store=MergeStore())

    result = await adapter.prepare(
        workspace=tmp_path,
        request=_request(),
        policy=DeliveryPolicy(verify_command=("pytest",)),
    )

    assert result.status is PreparationStatus.READY
    assert result.bundle is not None
    assert result.bundle.bundle_hash == _bundle().bundle_hash
    assert result.verification is not None
    assert result.verification.output_excerpt == "1 passed"
    assert pipeline.prepare_calls == 1


@pytest.mark.asyncio
async def test_publisher_registers_merge_record_without_webhook(tmp_path: Path) -> None:
    pipeline = FakePipeline(tmp_path)
    merges = MergeStore()
    adapter = GitHubChangeAdapter(
        pipeline=pipeline,
        merge_store=merges,
        clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
    )
    bundle = _bundle()
    approval = ChangeApproval(
        approver="alice",
        trace_id="trace-1",
        bundle_hash=bundle.bundle_hash,
    )

    push = await adapter.publish(
        workspace=tmp_path,
        bundle=bundle,
        approval=approval,
    )

    record = merges.get(("owner/name", 5, "b" * 40))
    assert push.pull_request.number == 5
    assert record is not None
    assert record.state == MergeState.WATCHING.value
    assert record.transitions[0]["to"] == MergeState.WATCHING.value
    assert pipeline.publish_calls == 1


@pytest.mark.asyncio
async def test_adapter_refuses_a_workspace_different_from_pipeline(tmp_path: Path) -> None:
    pipeline = FakePipeline(tmp_path / "bound")
    adapter = GitHubChangeAdapter(pipeline=pipeline, merge_store=MergeStore())

    with pytest.raises(ValueError, match="workspace"):
        await adapter.prepare(
            workspace=tmp_path / "other",
            request=_request(),
            policy=DeliveryPolicy(verify_command=("pytest",)),
        )
