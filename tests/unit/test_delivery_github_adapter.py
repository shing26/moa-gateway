from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.pipeline import (
    ChangeApproval,
    ChangePipeline,
    ChangeResult,
    ChangeStatus,
)
from apps.code_review_pipeline.delivery.adapters.github import GitHubChangeAdapter
from apps.code_review_pipeline.delivery.flow import DeliveryFlow
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRunState,
    DeliveryUsage,
)
from apps.code_review_pipeline.delivery.ports import (
    PreparationStatus,
    PushRecoveryRequired,
)
from apps.code_review_pipeline.delivery.run_store import DeliveryRunStore
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
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
async def test_preparer_uses_bundle_full_output_hash(tmp_path: Path) -> None:
    full_hash = "f" * 64
    bundle = replace(
        _bundle(),
        verify_output="display excerpt",
        verify_output_hash=full_hash,
    )

    class HashPipeline(FakePipeline):
        async def prepare_bundle(self, request):
            self.prepare_calls += 1
            return ChangeResult(
                status=ChangeStatus.VERIFIED,
                bundle=bundle,
                message="ready",
            )

    adapter = GitHubChangeAdapter(
        pipeline=HashPipeline(tmp_path),
        merge_store=MergeStore(),
    )

    result = await adapter.prepare(
        workspace=tmp_path,
        request=_request(),
        policy=DeliveryPolicy(verify_command=("pytest",)),
    )

    assert result.verification is not None
    assert result.verification.output_hash == full_hash
    assert result.verification.output_excerpt == "display excerpt"


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
        run_id="run-1",
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
    assert record.run_id == "run-1"
    assert record.trace_id == "trace-1"
    assert record.bundle_hash == bundle.bundle_hash
    assert record.transitions[0]["run_id"] == "run-1"
    assert record.transitions[0]["trace_id"] == "trace-1"
    assert record.transitions[0]["bundle_hash"] == bundle.bundle_hash
    assert pipeline.publish_calls == 1


@pytest.mark.asyncio
async def test_publisher_upgrades_a_webhook_created_merge_record(tmp_path: Path) -> None:
    pipeline = FakePipeline(tmp_path)
    merges = MergeStore()
    identity = ("owner/name", 5, "b" * 40)
    merges.save(
        MergeRecord(
            repo=identity[0],
            pr_number=identity[1],
            head_sha=identity[2],
        )
    )
    adapter = GitHubChangeAdapter(
        pipeline=pipeline,
        merge_store=merges,
        clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
    )
    bundle = _bundle()

    await adapter.publish(
        workspace=tmp_path,
        bundle=bundle,
        approval=ChangeApproval(
            approver="alice",
            trace_id="trace-1",
            bundle_hash=bundle.bundle_hash,
            run_id="run-1",
        ),
    )

    record = merges.get(identity)
    assert record is not None
    assert record.run_id == "run-1"
    assert record.trace_id == "trace-1"
    assert record.bundle_hash == bundle.bundle_hash
    assert record.transitions[0]["run_id"] == "run-1"
    assert record.transitions[0]["trace_id"] == "trace-1"
    assert record.transitions[0]["bundle_hash"] == bundle.bundle_hash


@pytest.mark.asyncio
async def test_publish_preserves_push_when_merge_registration_fails(
    tmp_path: Path,
) -> None:
    class FailingSaveStore(MergeStore):
        def save(self, record) -> None:
            raise RuntimeError("merge store unavailable")

    adapter = GitHubChangeAdapter(
        pipeline=FakePipeline(tmp_path),
        merge_store=FailingSaveStore(),
        clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
    )
    bundle = _bundle()

    with pytest.raises(PushRecoveryRequired) as caught:
        await adapter.publish(
            workspace=tmp_path,
            bundle=bundle,
            approval=ChangeApproval(
                approver="alice",
                trace_id="trace-1",
                bundle_hash=bundle.bundle_hash,
                run_id="run-1",
            ),
        )

    assert caught.value.push.pull_request.number == 5
    assert caught.value.push.pull_request.head_sha == "b" * 40
    assert "merge store unavailable" in str(caught.value)


@pytest.mark.asyncio
async def test_merge_registration_recovery_is_idempotent(tmp_path: Path) -> None:
    class FlakySaveStore(MergeStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_saves = 1

        def save(self, record) -> None:
            if self.fail_saves:
                self.fail_saves -= 1
                raise RuntimeError("merge store unavailable")
            super().save(record)

    merges = FlakySaveStore()
    adapter = GitHubChangeAdapter(
        pipeline=FakePipeline(tmp_path),
        merge_store=merges,
        clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
    )
    bundle = _bundle()
    with pytest.raises(PushRecoveryRequired) as caught:
        await adapter.publish(
            workspace=tmp_path,
            bundle=bundle,
            approval=ChangeApproval(
                approver="alice",
                trace_id="trace-1",
                bundle_hash=bundle.bundle_hash,
                run_id="run-1",
            ),
        )

    first = adapter.recover(
        push=caught.value.push,
        run_id="run-1",
        trace_id="trace-1",
        bundle_hash=bundle.bundle_hash,
    )
    second = adapter.recover(
        push=caught.value.push,
        run_id="run-1",
        trace_id="trace-1",
        bundle_hash=bundle.bundle_hash,
    )

    assert first == second
    assert len(first.transitions) == 1
    assert merges.get(("owner/name", 5, "b" * 40)) == first


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


@pytest.mark.asyncio
async def test_delivery_run_approval_wal_and_merge_record_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")

    wal_entries = []

    async def capture_wal(entry) -> None:
        wal_entries.append(entry)

    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record",
        capture_wal,
    )

    class GitHub:
        async def commit_exists(self, repo, sha):
            return True

        async def create_ref(self, repo, branch, base_sha):
            return None

        async def create_commit(self, repo, **kwargs):
            return "b" * 40

        async def create_pull_request(self, repo, **kwargs):
            return PullRequestRef(
                number=5,
                url="https://example.com/pr/5",
                head_sha="b" * 40,
            )

    class Implementer:
        async def run(
            self,
            *,
            workspace: Path,
            goal: str,
            feedback: str,
            max_steps: int,
            max_tokens: int,
        ) -> AgentAttempt:
            (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            return AgentAttempt(
                role="implementer",
                status="completed",
                summary="updated value",
                stop_reason="model finished",
                usage=DeliveryUsage(llm_calls=1, prompt_tokens=10, completion_tokens=2),
                steps=(),
            )

    class Approval:
        requested_run_id = ""

        async def request(self, *, run_id: str, bundle):
            self.requested_run_id = run_id
            return ChangeApproval(
                approver="alice",
                trace_id=bundle.trace_id,
                bundle_hash=bundle.bundle_hash,
                run_id=run_id,
            )

    class Audit:
        async def record(self, **kwargs):
            return None

    pipeline = ChangePipeline(
        github=GitHub(),
        repo="owner/name",
        workspace=repo,
        verify_command=(sys.executable, "-c", "raise SystemExit(0)"),
        model_name="test-model",
        push_enabled=True,
    )
    merges = MergeStore()
    adapter = GitHubChangeAdapter(
        pipeline=pipeline,
        merge_store=merges,
        clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
    )
    run_store = DeliveryRunStore(tmp_path / "runs")
    approval = Approval()
    request = _request()
    flow = DeliveryFlow(
        workspace=repo,
        policy=DeliveryPolicy(
            verify_command=(sys.executable, "-c", "raise SystemExit(0)"),
        ),
        implementer=Implementer(),
        preparer=adapter,
        approval=approval,
        publisher=adapter,
        recorder=run_store,
        audit=Audit(),
        clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
        new_run_id=lambda: "run-e2e",
    )

    run = await flow.run(request)
    saved = run_store.load(run.run_id)
    view = run_store.view(saved, merges)
    human = next(
        entry for entry in wal_entries if entry.agent_name == "human_decision"
    )

    assert run.state is DeliveryRunState.WAITING_CI
    assert approval.requested_run_id == "run-e2e"
    assert saved.run_id == run.run_id
    assert saved.push == run.push
    assert saved.bundle is not None
    assert view.merge is not None
    assert view.merge.run_id == run.run_id
    assert view.merge.trace_id == run.trace_id
    assert view.merge.bundle_hash == saved.bundle.bundle_hash
    assert view.merge.transitions[0]["run_id"] == run.run_id
    assert view.merge.transitions[0]["trace_id"] == run.trace_id
    assert view.merge.transitions[0]["bundle_hash"] == saved.bundle.bundle_hash
    assert human.trace_id == run.trace_id
    assert human.extra["run_id"] == run.run_id
    assert human.extra["trace_id"] == run.trace_id
    assert human.extra["bundle_hash"] == saved.bundle.bundle_hash
