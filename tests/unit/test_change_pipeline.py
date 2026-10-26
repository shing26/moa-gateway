from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from apps.code_review_pipeline.change import (
    ChangeApproval,
    ChangePipeline,
    ChangeRequest,
    ChangeStatus,
    build_change_bundle,
)


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "init"], root)
    return root


def _request() -> ChangeRequest:
    return ChangeRequest(
        repo="owner/name",
        base_sha="a" * 40,
        title="fix: correct add",
        body="agent change",
        goal="fix add",
        branch="moa/change-1",
        trace_id="trace-1",
    )


def _passing() -> tuple[str, ...]:
    return (sys.executable, "-c", "raise SystemExit(0)")


def _failing() -> tuple[str, ...]:
    return (sys.executable, "-c", "raise SystemExit(1)")


def test_verify_failure_blocks_export(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    pipeline = ChangePipeline(
        github=object(),
        repo="owner/name",
        workspace=repo,
        verify_command=_failing(),
        model_name="test-model",
    )

    import asyncio

    result = asyncio.run(pipeline.run(_request()))

    assert result.status is ChangeStatus.VERIFY_FAILED
    assert result.ok is False
    assert result.bundle is not None
    assert result.bundle.patch.strip()


def test_no_changes_is_not_exportable(repo: Path) -> None:
    pipeline = ChangePipeline(
        github=object(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
    )

    import asyncio

    result = asyncio.run(pipeline.run(_request()))

    assert result.status is ChangeStatus.NO_CHANGES
    assert result.ok is False


def test_dry_run_is_default_and_has_no_side_effect(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    class ExplodingGitHub:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"dry-run 不应调用 GitHub: {name}")

    pipeline = ChangePipeline(
        github=ExplodingGitHub(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
        push_enabled=False,
    )

    import asyncio

    result = asyncio.run(pipeline.run(_request()))

    assert result.status is ChangeStatus.PUSH_DISABLED
    assert result.pr_url == ""
    assert result.bundle is not None and result.bundle.is_verified


def test_push_requires_approval_even_when_enabled(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    class ExplodingGitHub:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"未审批不应调用 GitHub: {name}")

    pipeline = ChangePipeline(
        github=ExplodingGitHub(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
        push_enabled=True,
    )

    import asyncio

    result = asyncio.run(pipeline.run(_request()))

    assert result.status is ChangeStatus.NOT_APPROVED
    assert result.pr_url == ""


def test_push_rejects_approval_without_approver(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    class ExplodingGitHub:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"无审批人不應调用 GitHub: {name}")

    pipeline = ChangePipeline(
        github=ExplodingGitHub(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
        push_enabled=True,
    )

    import asyncio

    result = asyncio.run(
        pipeline.run(
            _request(),
            approval=ChangeApproval(
                approver="  ",
                trace_id="trace-1",
                bundle_hash="unused",
            ),
        )
    )

    assert result.status is ChangeStatus.NOT_APPROVED
    assert "approver" in result.message.lower()


def test_push_rejects_approval_for_a_different_bundle(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    class ExplodingGitHub:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"错误 bundle hash 不应调用 GitHub: {name}")

    pipeline = ChangePipeline(
        github=ExplodingGitHub(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
        push_enabled=True,
    )

    import asyncio

    result = asyncio.run(
        pipeline.run(
            _request(),
            approval=ChangeApproval(
                approver="alice",
                trace_id="trace-1",
                bundle_hash="wrong-bundle",
            ),
        )
    )

    assert result.status is ChangeStatus.NOT_APPROVED
    assert "bundle" in result.message.lower()


def test_push_rejects_approval_for_a_different_trace(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    request = _request()

    class ExplodingGitHub:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"错误 trace_id 不应调用 GitHub: {name}")

    common = {
        "repo": "owner/name",
        "workspace": repo,
        "verify_command": _passing(),
        "model_name": "test-model",
    }
    import asyncio

    dry_run = asyncio.run(
        ChangePipeline(github=object(), push_enabled=False, **common).run(request)
    )
    assert dry_run.bundle is not None

    result = asyncio.run(
        ChangePipeline(
            github=ExplodingGitHub(),
            push_enabled=True,
            **common,
        ).run(
            request,
            approval=ChangeApproval(
                approver="alice",
                trace_id="different-trace",
                bundle_hash=dry_run.bundle.bundle_hash,
            ),
        )
    )

    assert result.status is ChangeStatus.NOT_APPROVED
    assert "trace" in result.message.lower()


def test_push_records_approval_before_any_remote_write(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    request = _request()
    events: list[tuple[str, dict[str, str]]] = []

    async def record_approval(**kwargs: str) -> None:
        events.append(("approval", kwargs))

    class RecordingGitHub:
        async def commit_exists(self, repo, sha):
            return True

        async def create_ref(self, repo, branch, base_sha):
            events.append(("ref", {"branch": branch}))

        async def create_commit(self, repo, **kwargs):
            events.append(("commit", {"branch": kwargs["branch"]}))
            return "b" * 40

        async def create_pull_request(self, repo, **kwargs):
            events.append(("pr", {"branch": kwargs["branch"]}))
            return "https://example.com/pr/1"

    common = {
        "repo": "owner/name",
        "workspace": repo,
        "verify_command": _passing(),
        "model_name": "test-model",
    }
    import asyncio

    dry_run = asyncio.run(
        ChangePipeline(github=object(), push_enabled=False, **common).run(request)
    )
    assert dry_run.bundle is not None

    result = asyncio.run(
        ChangePipeline(
            github=RecordingGitHub(),
            push_enabled=True,
            approval_recorder=record_approval,
            **common,
        ).run(
            request,
            approval=ChangeApproval(
                approver="alice",
                trace_id="trace-1",
                bundle_hash=dry_run.bundle.bundle_hash,
            ),
        )
    )

    assert result.status is ChangeStatus.PUSHED
    assert events[0] == (
        "approval",
        {
            "task_id": "trace-1",
            "repo": "owner/name",
            "operator": "alice",
            "decision": f"approve_change:{dry_run.bundle.bundle_hash}",
        },
    )
    assert [name for name, _ in events[1:]] == ["ref", "commit", "pr"]


def test_push_calls_only_three_write_operations(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    request = _request()
    calls: list[tuple[str, dict]] = []

    class RecordingGitHub:
        async def commit_exists(self, repo, sha):
            return True

        async def create_ref(self, repo, branch, base_sha):
            calls.append(("ref", {"branch": branch, "base_sha": base_sha}))

        async def create_commit(self, repo, **kwargs):
            calls.append(("commit", kwargs))
            return "b" * 40

        async def create_pull_request(self, repo, **kwargs):
            calls.append(("pr", kwargs))
            return "https://example.com/pr/1"

    pipeline = ChangePipeline(
        github=RecordingGitHub(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
        push_enabled=True,
    )

    import asyncio

    dry_run = asyncio.run(
        ChangePipeline(
            github=object(),
            repo="owner/name",
            workspace=repo,
            verify_command=_passing(),
            model_name="test-model",
            push_enabled=False,
        ).run(request)
    )
    assert dry_run.bundle is not None

    result = asyncio.run(
        pipeline.run(
            request,
            approval=ChangeApproval(
                approver="alice",
                trace_id="trace-1",
                bundle_hash=dry_run.bundle.bundle_hash,
            ),
        )
    )

    assert result.status is ChangeStatus.PUSHED
    assert result.pr_url == "https://example.com/pr/1"
    assert [name for name, _ in calls] == ["ref", "commit", "pr"]
    files = calls[1][1]["files"]
    assert [item["path"] for item in files] == ["app.py"]
    assert result.audit["approver"] == "alice"


def test_push_rejects_base_sha_missing_from_remote(repo: Path) -> None:
    """本地临时 HEAD 不是远端 commit 时必须明确报错，而不是让它变成 422。"""
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    class NoCommitGitHub:
        async def commit_exists(self, repo, sha):
            return False

        async def create_commit(self, *args, **kwargs):
            raise AssertionError("不应在校验前建 commit")

        async def create_pull_request(self, *args, **kwargs):
            raise AssertionError("不应在校验前开 PR")

        async def create_ref(self, *args, **kwargs):
            raise AssertionError("不应在 base_sha 校验前建分支")

    pipeline = ChangePipeline(
        github=NoCommitGitHub(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
        push_enabled=True,
    )

    import asyncio

    dry_run = asyncio.run(
        ChangePipeline(
            github=object(),
            repo="owner/name",
            workspace=repo,
            verify_command=_passing(),
            model_name="test-model",
            push_enabled=False,
        ).run(_request())
    )
    assert dry_run.bundle is not None

    with pytest.raises(ValueError, match="不存在于远端仓库"):
        asyncio.run(
            pipeline.run(
                _request(),
                approval=ChangeApproval(
                    approver="alice",
                    trace_id="trace-1",
                    bundle_hash=dry_run.bundle.bundle_hash,
                ),
            )
        )


def test_missing_verify_command_refuses_to_export(repo: Path) -> None:
    pipeline = ChangePipeline(
        github=object(),
        repo="owner/name",
        workspace=repo,
        verify_command=(),
        model_name="test-model",
    )

    import asyncio

    result = asyncio.run(pipeline.run(_request()))

    assert result.status is ChangeStatus.NO_VERIFY_COMMAND
    assert result.ok is False


def test_missing_model_refuses_to_export(repo: Path) -> None:
    pipeline = ChangePipeline(
        github=object(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="  ",
    )

    import asyncio

    result = asyncio.run(pipeline.run(_request()))

    assert result.status is ChangeStatus.MODEL_NOT_CONFIGURED
    assert result.ok is False


def test_bundle_records_diff_and_hashes(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    bundle = build_change_bundle(
        workspace=repo,
        base_sha=_git(["rev-parse", "HEAD"], repo),
        branch="moa/change-1",
        title="fix: correct add",
        body="agent change",
        verify_command=_passing(),
        verify_exit_code=0,
        verify_output="",
        model="test-model",
    )

    assert bundle.is_verified is True
    assert bundle.patch_hash
    assert bundle.bundle_hash
    assert [item.path for item in bundle.files] == ["app.py"]
    assert bundle.files[0].kind == "modified"
    assert bundle.files[0].before_hash
    assert bundle.files[0].after_hash


def test_bundle_includes_added_files_and_skips_cache_artifacts(repo: Path) -> None:
    (repo / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "junk.pyc").write_bytes(b"\x00\x01")

    bundle = build_change_bundle(
        workspace=repo,
        base_sha=_git(["rev-parse", "HEAD"], repo),
        branch="moa/change-2",
        title="feat: add module",
        body="",
        verify_command=_passing(),
        verify_exit_code=0,
        verify_output="",
        model="test-model",
    )

    assert [item.path for item in bundle.files] == ["new_module.py"]
    assert bundle.files[0].kind == "added"
    assert "new_module.py" in bundle.patch
    assert "__pycache__" not in bundle.patch


def test_bundle_hash_covers_full_diff_when_display_is_truncated(repo: Path) -> None:
    (repo / "app.py").write_text("x" * 250_000, encoding="utf-8")
    full_diff = subprocess.run(  # noqa: S603
        ["git", "diff", "--no-color", "--binary", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    bundle = build_change_bundle(
        workspace=repo,
        base_sha=_git(["rev-parse", "HEAD"], repo),
        branch="moa/change-large",
        title="test: large diff",
        body="",
        verify_command=_passing(),
        verify_exit_code=0,
        verify_output="",
        model="test-model",
    )

    assert "diff 已截断" in bundle.patch
    assert bundle.patch_hash == hashlib.sha256(full_diff.encode("utf-8")).hexdigest()


def test_push_rejects_workspace_content_changed_after_bundle(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    request = _request()

    async def mutate_after_bundle(**kwargs: str) -> None:
        (repo / "app.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")

    class NoRemoteWriteGitHub:
        async def commit_exists(self, repo, sha):
            return True

        async def create_ref(self, *args, **kwargs):
            raise AssertionError("内容已变化时不应创建远端分支")

        async def create_commit(self, *args, **kwargs):
            raise AssertionError("内容已变化时不应创建 commit")

        async def create_pull_request(self, *args, **kwargs):
            raise AssertionError("内容已变化时不应创建 PR")

    common = {
        "repo": "owner/name",
        "workspace": repo,
        "verify_command": _passing(),
        "model_name": "test-model",
    }
    import asyncio

    dry_run = asyncio.run(
        ChangePipeline(github=object(), push_enabled=False, **common).run(request)
    )
    assert dry_run.bundle is not None

    with pytest.raises(ValueError, match="changed after bundle"):
        asyncio.run(
            ChangePipeline(
                github=NoRemoteWriteGitHub(),
                push_enabled=True,
                approval_recorder=mutate_after_bundle,
                **common,
            ).run(
                request,
                approval=ChangeApproval(
                    approver="alice",
                    trace_id="trace-1",
                    bundle_hash=dry_run.bundle.bundle_hash,
                ),
            )
        )


def test_push_uses_traceable_branch_and_commit_message(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    request = _request()
    calls: dict[str, dict] = {}

    class RecordingGitHub:
        async def commit_exists(self, repo, sha):
            return True

        async def create_ref(self, repo, branch, base_sha):
            calls["ref"] = {"branch": branch, "base_sha": base_sha}

        async def create_commit(self, repo, **kwargs):
            calls["commit"] = kwargs
            return "b" * 40

        async def create_pull_request(self, repo, **kwargs):
            calls["pr"] = kwargs
            return "https://example.com/pr/1"

    async def record_approval(**kwargs: str) -> None:
        return None

    common = {
        "repo": "owner/name",
        "workspace": repo,
        "verify_command": _passing(),
        "model_name": "test-model",
    }
    import asyncio

    dry_run = asyncio.run(
        ChangePipeline(github=object(), push_enabled=False, **common).run(request)
    )
    assert dry_run.bundle is not None

    result = asyncio.run(
        ChangePipeline(
            github=RecordingGitHub(),
            push_enabled=True,
            approval_recorder=record_approval,
            **common,
        ).run(
            request,
            approval=ChangeApproval(
                approver="alice",
                trace_id="trace-1",
                bundle_hash=dry_run.bundle.bundle_hash,
            ),
        )
    )

    assert result.status is ChangeStatus.PUSHED
    assert calls["ref"]["branch"] == "moa/change-1-trace-1"
    message = calls["commit"]["message"]
    assert "MoA-Trace-Id: trace-1" in message
    assert f"MoA-Bundle-Hash: {dry_run.bundle.bundle_hash}" in message


def test_push_rejects_request_repo_that_differs_from_pipeline_repo(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    request = _request()

    class ExplodingGitHub:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"仓库不一致不应调用 GitHub: {name}")

    common = {
        "repo": "owner/name",
        "workspace": repo,
        "verify_command": _passing(),
        "model_name": "test-model",
    }
    import asyncio

    dry_run = asyncio.run(
        ChangePipeline(github=object(), push_enabled=False, **common).run(request)
    )
    assert dry_run.bundle is not None

    with pytest.raises(ValueError, match="request repo"):
        asyncio.run(
            ChangePipeline(
                github=ExplodingGitHub(),
                push_enabled=True,
                **common,
            ).run(
                replace(request, repo="other/name"),
                approval=ChangeApproval(
                    approver="alice",
                    trace_id="trace-1",
                    bundle_hash=dry_run.bundle.bundle_hash,
                ),
            )
        )


def test_push_refuses_when_remote_commit_probe_is_unavailable(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    request = _request()

    class NoCommitProbeGitHub:
        async def create_ref(self, *args, **kwargs):
            raise AssertionError("无法验证 base sha 时不应创建远端分支")

        async def create_commit(self, *args, **kwargs):
            raise AssertionError("无法验证 base sha 时不应创建 commit")

        async def create_pull_request(self, *args, **kwargs):
            raise AssertionError("无法验证 base sha 时不应创建 PR")

    common = {
        "repo": "owner/name",
        "workspace": repo,
        "verify_command": _passing(),
        "model_name": "test-model",
    }
    import asyncio

    dry_run = asyncio.run(
        ChangePipeline(github=object(), push_enabled=False, **common).run(request)
    )
    assert dry_run.bundle is not None

    with pytest.raises(RuntimeError, match="commit_exists"):
        asyncio.run(
            ChangePipeline(
                github=NoCommitProbeGitHub(),
                push_enabled=True,
                **common,
            ).run(
                request,
                approval=ChangeApproval(
                    approver="alice",
                    trace_id="trace-1",
                    bundle_hash=dry_run.bundle.bundle_hash,
                ),
            )
        )


def test_push_requires_trace_id_for_traceable_export(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    request = replace(_request(), trace_id="")

    class NoRemoteWriteGitHub:
        async def commit_exists(self, repo, sha):
            return True

        async def create_ref(self, *args, **kwargs):
            raise AssertionError("缺少 trace_id 时不应创建远端分支")

        async def create_commit(self, *args, **kwargs):
            raise AssertionError("缺少 trace_id 时不应创建 commit")

        async def create_pull_request(self, *args, **kwargs):
            raise AssertionError("缺少 trace_id 时不应创建 PR")

    common = {
        "repo": "owner/name",
        "workspace": repo,
        "verify_command": _passing(),
        "model_name": "test-model",
    }
    import asyncio

    dry_run = asyncio.run(
        ChangePipeline(github=object(), push_enabled=False, **common).run(request)
    )
    assert dry_run.bundle is not None

    with pytest.raises(ValueError, match="trace_id"):
        asyncio.run(
            ChangePipeline(
                github=NoRemoteWriteGitHub(),
                push_enabled=True,
                **common,
            ).run(
                request,
                approval=ChangeApproval(
                    approver="alice",
                    trace_id="",
                    bundle_hash=dry_run.bundle.bundle_hash,
                ),
            )
        )


def test_bundle_hash_covers_commit_metadata_and_trace(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    bundle = build_change_bundle(
        workspace=repo,
        base_sha=_git(["rev-parse", "HEAD"], repo),
        branch="moa/change-1-trace-1",
        title="fix: correct add",
        body="first body",
        verify_command=_passing(),
        verify_exit_code=0,
        verify_output="",
        model="test-model",
        trace_id="trace-1",
    )
    variants = (
        replace(bundle, body="second body"),
        replace(bundle, title="fix: another title"),
        replace(bundle, files=(replace(bundle.files[0], after_hash="0" * 64),)),
        replace(bundle, model="another-model"),
        replace(bundle, trace_id="trace-2"),
    )

    assert all(item.bundle_hash != bundle.bundle_hash for item in variants)
