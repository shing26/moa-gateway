from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apps.code_review_pipeline.delivery.workspace import GitHubWorkspaceProvisioner


class FakeArchiveGitHub:
    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, str, Path]] = []
        self.files = files or {"app.py": "VALUE = 1\n"}

    async def download_archive(self, repo, ref: str, dest: Path) -> None:
        self.calls.append((repo.owner, ref, dest))
        dest.mkdir(parents=True, exist_ok=True)
        for relative_path, content in self.files.items():
            target = dest / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


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


@pytest.mark.asyncio
async def test_ignored_archive_files_are_tracked_in_baseline(tmp_path: Path) -> None:
    github = FakeArchiveGitHub(
        {
            ".gitignore": "ignored.py\n",
            "ignored.py": "VALUE = 1\n",
        }
    )
    provisioner = GitHubWorkspaceProvisioner(github=github)
    destination = tmp_path / "workspace"

    await provisioner.materialize(
        repo="owner/name",
        base_sha="a" * 40,
        destination=destination,
    )

    ignored = destination / "ignored.py"
    ignored.write_text("VALUE = 2\n", encoding="utf-8")
    status = subprocess.run(  # noqa: ASYNC221 - verifies tracked Git baseline state
        ["git", "status", "--porcelain"],
        cwd=destination,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() == "M ignored.py"


@pytest.mark.asyncio
async def test_materialize_rejects_repo_with_extra_slash(tmp_path: Path) -> None:
    github = FakeArchiveGitHub()
    provisioner = GitHubWorkspaceProvisioner(github=github)

    with pytest.raises(ValueError, match="owner/name"):
        await provisioner.materialize(
            repo="owner/name/extra",
            base_sha="a" * 40,
            destination=tmp_path / "workspace",
        )

    assert github.calls == []
