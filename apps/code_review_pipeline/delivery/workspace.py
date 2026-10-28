from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from app.agents.subprocess_env import harden_git_argv, isolated_env
from apps.code_review_pipeline.routing.github_client import GitHubRepo


def _git(args: list[str], *, cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 - arguments are constructed locally
        harden_git_argv(args),
        cwd=cwd,
        env=isolated_env(cwd, git=True),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.strip()


def _init_baseline(destination: Path) -> None:
    _git(["init", "-q"], cwd=destination)
    _git(["config", "user.email", "delivery@moa.local"], cwd=destination)
    _git(["config", "user.name", "MoA Delivery"], cwd=destination)
    _git(["add", "--force", "--all", "--", "."], cwd=destination)
    _git(["commit", "-q", "-m", "materialized base snapshot"], cwd=destination)


class GitHubWorkspaceProvisioner:
    def __init__(self, *, github: Any) -> None:
        self._github = github

    async def materialize(
        self,
        *,
        repo: str,
        base_sha: str,
        destination: Path,
    ) -> Path:
        owner, separator, name = repo.partition("/")
        if repo.count("/") != 1 or not separator or not owner.strip() or not name.strip():
            raise ValueError(f"repo must be owner/name: {repo!r}")
        root = Path(destination).resolve()
        if root.exists() and any(root.iterdir()):
            raise ValueError(f"destination must be empty: {root}")
        root.mkdir(parents=True, exist_ok=True)
        await self._github.download_archive(
            GitHubRepo(owner=owner, name=name),
            base_sha,
            root,
        )
        await asyncio.to_thread(_init_baseline, root)
        return root
