from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apps.code_review_pipeline.delivery.workspace import GitHubWorkspaceProvisioner


class FakeArchiveGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path]] = []

    async def download_archive(self, repo, ref: str, dest: Path) -> None:
        self.calls.append((repo.owner, ref, dest))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "app.py").write_text("VALUE = 1\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_workspace_is_a_local_git_baseline(tmp_path: Path) -> None:
    github = FakeArchiveGitHub()
    provisioner = GitHubWorkspaceProvisioner(github=github)
    destination = tmp_path / "workspace"

    await provisioner.materialize(
        repo="owner/name",
        base_sha="a" * 40,
        destination=destination,
    )

    assert github.calls[0][:2] == ("owner", "a" * 40)
    assert (destination / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    status = subprocess.run(  # noqa: ASYNC221 - verifies the synchronous Git baseline
        ["git", "status", "--porcelain"],
        cwd=destination,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""
