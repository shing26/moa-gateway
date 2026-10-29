"""PR 审查工作区：下载、测试命令探测与清理。"""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.config import settings
from apps.code_review_pipeline.archive import extract_repo_tarball
from apps.code_review_pipeline.routing.github_client import GitHubRepo
from apps.code_review_pipeline.schemas.pr_context import PRContext

logger = logging.getLogger("moa.code_review.workspace")

_PYTHON_MARKERS = ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "tests")


@dataclass(frozen=True)
class ReviewWorkspace:
    path: Path | None
    verify_command: tuple[str, ...] = ()
    error: str = ""


class WorkspaceProvisioner(Protocol):
    async def prepare(self, pr: PRContext) -> ReviewWorkspace: ...

    async def cleanup(self, workspace: ReviewWorkspace) -> None: ...


def detect_verify_command(workspace: Path) -> tuple[str, ...]:
    """先看显式配置，再对 Python 仓库回退到 pytest。"""
    configured = str(
        getattr(settings, "code_review_verify_command", "") or ""
    ).strip()
    if configured:
        return tuple(shlex.split(configured))
    if any((workspace / marker).exists() for marker in _PYTHON_MARKERS):
        return (sys.executable, "-m", "pytest", "-q")
    return ()


class GitHubArchiveWorkspaceProvisioner:
    """用 GitHub tarball API 准备一个隔离工作区。"""

    def __init__(
        self,
        client: Any,
        *,
        root: Path | None = None,
        verify_command: Sequence[str] | None = None,
    ) -> None:
        self._client = client
        self._root = root
        self._verify_command = (
            tuple(str(token) for token in verify_command)
            if verify_command is not None
            else None
        )

    async def prepare(self, pr: PRContext) -> ReviewWorkspace:
        download = getattr(self._client, "download_archive", None)
        if download is None:
            return ReviewWorkspace(
                path=None,
                error="GitHub 客户端不支持下载仓库归档",
            )

        root = self._root
        if root is None:
            configured = str(
                getattr(settings, "code_review_workspace_root", "") or ""
            ).strip()
            root = Path(configured) if configured else None
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)

        path = Path(
            tempfile.mkdtemp(
                prefix="moa-review-",
                dir=str(root) if root is not None else None,
            )
        )
        try:
            owner, _, name = pr.repo.partition("/")
            await download(
                GitHubRepo(owner=owner, name=name or pr.repo),
                pr.head_sha or "HEAD",
                path,
            )
            command = (
                self._verify_command
                if self._verify_command is not None
                else detect_verify_command(path)
            )
            if not command:
                return ReviewWorkspace(
                    path=path,
                    error="未检测到可用的测试命令",
                )
            return ReviewWorkspace(path=path, verify_command=tuple(command))
        except Exception as exc:
            shutil.rmtree(path, ignore_errors=True)
            logger.warning("workspace provision failed for %s#%s: %s", pr.repo, pr.pr_number, exc)
            return ReviewWorkspace(
                path=None,
                error=f"workspace provision failed: {exc}",
            )

    async def cleanup(self, workspace: ReviewWorkspace) -> None:
        if workspace.path is None:
            return
        await asyncio.to_thread(shutil.rmtree, workspace.path, True)


__all__ = [
    "GitHubArchiveWorkspaceProvisioner",
    "ReviewWorkspace",
    "WorkspaceProvisioner",
    "detect_verify_command",
    "extract_repo_tarball",
]
