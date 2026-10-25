from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from apps.code_review_pipeline.change import (
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

    result = asyncio.run(pipeline.run(_request(), approved=True))

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

    result = asyncio.run(pipeline.run(_request(), approved=False))

    assert result.status is ChangeStatus.NOT_APPROVED
    assert result.pr_url == ""


def test_push_calls_only_three_write_operations(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    calls: list[tuple[str, dict]] = []

    class RecordingGitHub:
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

    result = asyncio.run(pipeline.run(_request(), approved=True, approver="alice"))

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

    with pytest.raises(ValueError, match="不存在于远端仓库"):
        asyncio.run(pipeline.run(_request(), approved=True, approver="alice"))


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
