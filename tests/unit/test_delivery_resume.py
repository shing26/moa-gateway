from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cli import build_parser, cmd_delivery_resume
from apps.code_review_pipeline.change.bundle import (
    ChangeBundle,
    build_change_bundle,
)
from apps.code_review_pipeline.change.pipeline import ChangeApproval
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.delivery import resume as resume_module
from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    FailureCode,
    PushOutcome,
)
from apps.code_review_pipeline.delivery.resume import (
    ResumeOutcome,
    _apply_recorded_patch,
    resume_run,
)
from apps.code_review_pipeline.delivery.run_store import DeliveryRunStore
from apps.code_review_pipeline.delivery.workspace import (
    GitHubWorkspaceProvisioner,
)
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord

# 与实现者写工作区的方式一致：LF 字节，这正是 autocrlf 陷阱的根源。
BASE_CONTENT = "def add(a, b):\n    return a + b\n"
CHANGED_CONTENT = "def add(a, b):\n    return a + b + 0\n"


class _FakeGithub:
    """只实现 provisioner 需要的一个方法：把 base 快照解到目标目录。"""

    async def download_archive(self, repo, base_sha, destination) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "app.py").write_bytes(BASE_CONTENT.encode("utf-8"))

    async def aclose(self) -> None:
        return None


async def _materialize(destination: Path) -> Path:
    return await GitHubWorkspaceProvisioner(github=_FakeGithub()).materialize(
        repo="owner/name",
        base_sha="a" * 40,
        destination=destination,
    )


def _recorded_bundle(tmp_path: Path) -> ChangeBundle:
    """在真实 git 工作区里复刻"实现者改完、已验证、待审批"那一刻的包。"""

    async def build() -> ChangeBundle:
        workspace = await _materialize(tmp_path / "record-src")
        (workspace / "app.py").write_bytes(CHANGED_CONTENT.encode("utf-8"))
        return build_change_bundle(
            workspace=workspace,
            base_sha="a" * 40,
            branch="moa/fix-trace-1",
            title="fix: add",
            body="",
            verify_command=("python", "-m", "pytest", "-q"),
            verify_exit_code=0,
            verify_output="1 passed",
            model="test-model",
            trace_id="trace-1",
        )

    return asyncio.run(build())


def _synthetic_bundle() -> ChangeBundle:
    patch = "diff --git a/app.py b/app.py\n"
    return ChangeBundle(
        base_sha="a" * 40,
        branch="moa/fix-trace-1",
        title="fix: add",
        body="",
        files=(),
        patch=patch,
        patch_hash=hashlib.sha256(patch.encode()).hexdigest(),
        verify_command=("python", "-m", "pytest", "-q"),
        verify_exit_code=0,
        verify_output="1 passed",
        model="test-model",
        trace_id="trace-1",
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
    )


def _run(
    state: DeliveryRunState = DeliveryRunState.READY_FOR_PUSH_APPROVAL,
    *,
    bundle: ChangeBundle | None = None,
    push: PushOutcome | None = None,
) -> DeliveryRun:
    now = datetime(2026, 10, 27, tzinfo=UTC)
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
        bundle=bundle,
        push=push,
        merge=None,
        usage=DeliveryUsage(),
        failure=None,
        started_at=now,
        updated_at=now,
    )


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        delivery_run_record_dir=str(tmp_path / "runs"),
        change_agent_verify_timeout_s=900.0,
        change_agent_base_branch="main",
    )


class _FakeMergeStore:
    def __init__(self, record: MergeRecord | None = None) -> None:
        self._record = record
        self.saved: list[MergeRecord] = []

    def get(self, identity) -> MergeRecord | None:
        return self._record

    def save(self, record: MergeRecord) -> None:
        self.saved.append(record)

    def supersede_older(self, *args, **kwargs) -> None:
        pass

    async def close(self) -> None:
        return None


class _FakeApprovalPort:
    def __init__(self, approval: ChangeApproval | None) -> None:
        self._approval = approval
        self.requests: list[tuple[str, ChangeBundle]] = []

    async def request(self, *, run_id: str, bundle: ChangeBundle):
        self.requests.append((run_id, bundle))
        return self._approval


class _FakePipeline:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class _FakeAdapter:
    def __init__(self, *, pipeline, merge_store, error: Exception | None = None):
        self.pipeline = pipeline
        self.merge_store = merge_store
        self._error = error
        self.calls: list[dict] = []

    async def publish(self, *, workspace: Path, bundle: ChangeBundle, approval):
        if self._error is not None:
            raise self._error
        self.calls.append(
            {
                "workspace": workspace,
                "workspace_existed": workspace.is_dir(),
                "bundle": bundle,
                "approval": approval,
            }
        )
        return PushOutcome(
            branch=bundle.branch,
            commit_sha="b" * 40,
            pull_request=PullRequestRef(
                number=9,
                url="https://example.com/pr/9",
                head_sha="b" * 40,
            ),
        )


def _wire_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: Exception | None = None,
) -> list[_FakeAdapter]:
    adapters: list[_FakeAdapter] = []

    def make_adapter(*, pipeline, merge_store) -> _FakeAdapter:
        adapter = _FakeAdapter(
            pipeline=pipeline, merge_store=merge_store, error=error
        )
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(resume_module, "ChangePipeline", _FakePipeline)
    monkeypatch.setattr(resume_module, "GitHubChangeAdapter", make_adapter)
    return adapters


async def _save(store: DeliveryRunStore, run: DeliveryRun) -> None:
    await store.save(run)


def _record_state(tmp_path: Path) -> str:
    payload = json.loads(
        (tmp_path / "runs" / "run-1.json").read_text(encoding="utf-8")
    )
    return str(payload["state"])


# ---------------------------------------------------------------------------
# 状态分派
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        DeliveryRunState.WAITING_CI,
        DeliveryRunState.AWAITING_MERGE_APPROVAL,
        DeliveryRunState.MERGING,
    ],
)
def test_reconcile_states_overlay_merge_store(
    tmp_path: Path,
    state: DeliveryRunState,
) -> None:
    store = DeliveryRunStore(tmp_path / "runs")
    push = PushOutcome(
        branch="moa/fix-trace-1",
        commit_sha="b" * 40,
        pull_request=PullRequestRef(
            number=9, url="https://example.com/pr/9", head_sha="b" * 40
        ),
    )
    asyncio.run(_save(store, _run(state, push=push)))
    merge_store = _FakeMergeStore(
        MergeRecord(
            repo="owner/name",
            pr_number=9,
            head_sha="b" * 40,
            state=MergeState.MERGED.value,
            merged_sha="c" * 40,
        )
    )

    outcome = asyncio.run(
        resume_run(
            "run-1",
            store=store,
            github=_FakeGithub(),
            merge_store=merge_store,
            settings=_settings(tmp_path),
            approval_port=_FakeApprovalPort(None),
        )
    )

    assert outcome.exit_code == 0
    assert outcome.view is not None
    assert outcome.view["state"] == "merged"
    assert outcome.view["merge"]["merged_sha"] == "c" * 40
    persisted = store.load("run-1")
    assert persisted.state is DeliveryRunState.MERGED
    # reconcile 分支不触碰门禁：不需要任何 fake adapter。


def test_gate_reentry_state_is_only_ready_for_push_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapters = _wire_fakes(monkeypatch)
    store = DeliveryRunStore(tmp_path / "runs")

    for state in (
        DeliveryRunState.PREPARING,
        DeliveryRunState.IMPLEMENTING,
        DeliveryRunState.CHECKING,
        DeliveryRunState.VERIFIER_REVIEW,
        DeliveryRunState.REVISING,
        DeliveryRunState.PUSHING,
        DeliveryRunState.MERGED,
        DeliveryRunState.REJECTED,
        DeliveryRunState.NEEDS_HUMAN,
        DeliveryRunState.BUDGET_EXHAUSTED,
        DeliveryRunState.FAILED,
    ):
        run = _run(state)
        asyncio.run(_save(store, run))
        before = (tmp_path / "runs" / "run-1.json").read_text(encoding="utf-8")

        outcome = asyncio.run(
            resume_run(
                "run-1",
                store=store,
                github=_FakeGithub(),
                merge_store=_FakeMergeStore(),
                settings=_settings(tmp_path),
                approval_port=_FakeApprovalPort(None),
            )
        )

        assert outcome.exit_code == 2, state
        assert outcome.refused is True
        assert "run state is" in outcome.reason
        assert (
            tmp_path / "runs" / "run-1.json"
        ).read_text(encoding="utf-8") == before
    assert adapters == []


# ---------------------------------------------------------------------------
# 门禁重入：证据核对
# ---------------------------------------------------------------------------


def test_tampered_bundle_hash_refuses_before_anything_happens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapters = _wire_fakes(monkeypatch)
    store = DeliveryRunStore(tmp_path / "runs")
    asyncio.run(_save(store, _run(bundle=_synthetic_bundle())))
    record_path = tmp_path / "runs" / "run-1.json"
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["bundle"]["bundle_hash"] = "0" * 64
    record_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    before = record_path.read_text(encoding="utf-8")

    outcome = asyncio.run(
        resume_run(
            "run-1",
            store=store,
            github=_FakeGithub(),
            merge_store=_FakeMergeStore(),
            settings=_settings(tmp_path),
            approval_port=_FakeApprovalPort(None),
        )
    )

    assert outcome.exit_code == 2
    assert outcome.refused is True
    assert "bundle_hash mismatch" in outcome.reason
    # 记录一个字节都没动，门禁一步都没走。
    assert record_path.read_text(encoding="utf-8") == before
    assert _record_state(tmp_path) == "ready_for_push_approval"
    assert adapters == []


# ---------------------------------------------------------------------------
# 门禁重入：工作区复现、审批、发布
# ---------------------------------------------------------------------------


def test_non_tty_denial_leaves_record_ready_for_push_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapters = _wire_fakes(monkeypatch)
    store = DeliveryRunStore(tmp_path / "runs")
    bundle = _recorded_bundle(tmp_path)
    asyncio.run(_save(store, _run(bundle=bundle)))
    before = (tmp_path / "runs" / "run-1.json").read_text(encoding="utf-8")

    outcome = asyncio.run(
        resume_run(
            "run-1",
            store=store,
            github=_FakeGithub(),
            merge_store=_FakeMergeStore(),
            settings=_settings(tmp_path),
            approval_port=_FakeApprovalPort(None),
        )
    )

    assert outcome.exit_code == 1
    assert outcome.refused is False
    assert outcome.reason == "approval denied; nothing pushed"
    # 记录留在原地等下一次人来批。
    assert (tmp_path / "runs" / "run-1.json").read_text(encoding="utf-8") == before
    assert _record_state(tmp_path) == "ready_for_push_approval"
    # 门禁在这一步之前就结束了：publisher 根本没被构造出来。
    assert adapters == []


def test_happy_path_publishes_the_recorded_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapters = _wire_fakes(monkeypatch)
    store = DeliveryRunStore(tmp_path / "runs")
    bundle = _recorded_bundle(tmp_path)
    run = _run(bundle=bundle)
    asyncio.run(_save(store, run))
    approval = ChangeApproval(
        approver="alice",
        trace_id=bundle.trace_id,
        bundle_hash=bundle.bundle_hash,
        run_id="run-1",
    )
    port = _FakeApprovalPort(approval)

    outcome = asyncio.run(
        resume_run(
            "run-1",
            store=store,
            github=_FakeGithub(),
            merge_store=_FakeMergeStore(),
            settings=_settings(tmp_path),
            approval_port=port,
        )
    )

    assert outcome.exit_code == 0
    # 门禁看到的是 run_id + 记录里的那份包。
    assert port.requests == [("run-1", bundle)]
    # publish 收到的 bundle 与记录里的对象逐字段相等，工作区确实重新落地过。
    assert len(adapters) == 1
    assert len(adapters[0].calls) == 1
    call = adapters[0].calls[0]
    assert call["bundle"] == bundle
    # publish 时工作区确实存在（断言在 TemporaryDirectory 释放前记录）。
    assert call["workspace_existed"] is True
    pipeline_kwargs = adapters[0].pipeline.kwargs
    assert pipeline_kwargs["repo"] == "owner/name"
    assert pipeline_kwargs["push_enabled"] is True
    assert pipeline_kwargs["base_branch"] == "main"
    assert pipeline_kwargs["verify_command"] == bundle.verify_command
    # 记录推进到 waiting_ci，push 结果落盘。
    persisted = store.load("run-1")
    assert persisted.state is DeliveryRunState.WAITING_CI
    assert persisted.push is not None
    assert persisted.push.pull_request.number == 9
    payload = json.loads(
        (tmp_path / "runs" / "run-1.json").read_text(encoding="utf-8")
    )
    assert payload["push"]["pull_request"]["url"] == "https://example.com/pr/9"


def test_publish_failure_lands_failed_with_push_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapters = _wire_fakes(monkeypatch, error=RuntimeError("github 403"))
    store = DeliveryRunStore(tmp_path / "runs")
    bundle = _recorded_bundle(tmp_path)
    asyncio.run(_save(store, _run(bundle=bundle)))
    approval = ChangeApproval(
        approver="alice",
        trace_id=bundle.trace_id,
        bundle_hash=bundle.bundle_hash,
        run_id="run-1",
    )

    outcome = asyncio.run(
        resume_run(
            "run-1",
            store=store,
            github=_FakeGithub(),
            merge_store=_FakeMergeStore(),
            settings=_settings(tmp_path),
            approval_port=_FakeApprovalPort(approval),
        )
    )

    assert outcome.exit_code == 2
    assert "github 403" in outcome.reason
    persisted = store.load("run-1")
    assert persisted.state is DeliveryRunState.FAILED
    assert persisted.failure is not None
    assert persisted.failure.code is FailureCode.PUSH_FAILED
    assert adapters[0].calls == []


# ---------------------------------------------------------------------------
# autocrlf 回归：为什么必须先钉死 core.autocrlf=false 再 apply
# ---------------------------------------------------------------------------


def test_apply_recorded_patch_sets_autocrlf_false_before_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        resume_module,
        "_git",
        lambda args, *, cwd: calls.append(args) or "",
    )
    (tmp_path / "ws").mkdir()
    bundle = _synthetic_bundle()

    _apply_recorded_patch(tmp_path / "ws", bundle)

    assert calls[0] == ["config", "core.autocrlf", "false"]
    assert calls[1][:2] == ["apply", "--whitespace=nowarn"]


def test_rematerialized_workspace_reproduces_bundle_hash(tmp_path: Path) -> None:
    bundle = _recorded_bundle(tmp_path)
    patch_file = tmp_path / "bundle.patch"
    patch_file.write_bytes(bundle.patch.encode("utf-8"))

    # 反面教材：把 autocrlf 钉成 true 再 apply——工作区字节被漂移成 CRLF，
    # after_hash 变了，包哈希随之失配。这正是省掉那个 config 会发生的事。
    bad = asyncio.run(_materialize(tmp_path / "drifted"))
    subprocess.run(  # noqa: S603 - 测试本机固定参数
        [
            "git",
            "-c",
            "core.autocrlf=true",
            "apply",
            "--whitespace=nowarn",
            str(patch_file),
        ],
        cwd=bad,
        capture_output=True,
        text=True,
        check=True,
    )
    rebuilt_bad = build_change_bundle(
        workspace=bad,
        base_sha=bundle.base_sha,
        branch=bundle.branch,
        title=bundle.title,
        body=bundle.body,
        verify_command=bundle.verify_command,
        verify_exit_code=bundle.verify_exit_code,
        verify_output=bundle.verify_output,
        model=bundle.model,
        trace_id=bundle.trace_id,
        verify_output_hash=bundle.verify_output_hash,
    )

    # resume 自己的路径：先 core.autocrlf=false 再 apply，字节原样，
    # 包哈希必须复现。
    good = asyncio.run(_materialize(tmp_path / "clean"))
    _apply_recorded_patch(good, bundle)
    rebuilt_good = build_change_bundle(
        workspace=good,
        base_sha=bundle.base_sha,
        branch=bundle.branch,
        title=bundle.title,
        body=bundle.body,
        verify_command=bundle.verify_command,
        verify_exit_code=bundle.verify_exit_code,
        verify_output=bundle.verify_output,
        model=bundle.model,
        trace_id=bundle.trace_id,
        verify_output_hash=bundle.verify_output_hash,
    )

    assert rebuilt_bad.bundle_hash != bundle.bundle_hash
    assert rebuilt_good.bundle_hash == bundle.bundle_hash


# ---------------------------------------------------------------------------
# CLI 接线
# ---------------------------------------------------------------------------


def test_resume_parser_requires_run_id_and_wires_command() -> None:
    parser = build_parser()

    args = parser.parse_args(["delivery", "resume", "--run-id", "run-1"])

    assert args.group == "delivery"
    assert args.command == "resume"
    assert args.run_id == "run-1"
    assert args.func is cmd_delivery_resume


def test_cmd_delivery_resume_requires_github_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "github_token", "")
    args = build_parser().parse_args(["delivery", "resume", "--run-id", "run-1"])

    assert cmd_delivery_resume(args) == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_cmd_delivery_resume_requires_shared_merge_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings
    from apps.code_review_pipeline import merge_store as merge_store_module
    from apps.code_review_pipeline.merge_store import MergeStore

    monkeypatch.setattr(settings, "github_token", "token")
    monkeypatch.setattr(
        merge_store_module, "build_merge_store", lambda: MergeStore()
    )
    args = build_parser().parse_args(["delivery", "resume", "--run-id", "run-1"])

    assert cmd_delivery_resume(args) == 2
    assert "MergeStore" in capsys.readouterr().err


def test_cmd_delivery_resume_prints_reconcile_view_and_returns_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import settings
    from apps.code_review_pipeline import merge_store as merge_store_module
    from apps.code_review_pipeline.merge_store import PostgresMergeStore

    store = DeliveryRunStore(tmp_path / "runs")
    push = PushOutcome(
        branch="moa/fix-trace-1",
        commit_sha="b" * 40,
        pull_request=PullRequestRef(
            number=9, url="https://example.com/pr/9", head_sha="b" * 40
        ),
    )
    asyncio.run(_save(store, _run(DeliveryRunState.WAITING_CI, push=push)))
    monkeypatch.setattr(settings, "github_token", "token")
    monkeypatch.setattr(
        settings, "delivery_run_record_dir", str(tmp_path / "runs")
    )

    class _CloseOnlyGithub:
        async def aclose(self) -> None:
            return None

    async def fake_resume_run(run_id, **kwargs):
        return ResumeOutcome(
            run=kwargs["store"].load(run_id),
            exit_code=0,
            view=kwargs["store"].load(run_id).to_dict(),
        )

    monkeypatch.setattr(
        merge_store_module,
        "build_merge_store",
        lambda: PostgresMergeStore(dsn="postgresql://unused", _psycopg=object()),
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.routing.github_provider.build_github_client",
        lambda: _CloseOnlyGithub(),
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.delivery.resume.resume_run",
        fake_resume_run,
    )
    args = build_parser().parse_args(["delivery", "resume", "--run-id", "run-1"])

    assert cmd_delivery_resume(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "waiting_ci"
