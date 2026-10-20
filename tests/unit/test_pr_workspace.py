from __future__ import annotations

import shutil
import sys
import tarfile
from pathlib import Path

import pytest

from app.config import settings
from apps.code_review_pipeline.schemas.pr_context import PRContext


class CopyingArchiveClient:
    def __init__(self, source: Path, *, fail: bool = False) -> None:
        self.source = source
        self.fail = fail
        self.refs: list[str] = []
        self.destinations: list[Path] = []

    async def download_archive(self, repo: object, ref: str, dest: Path) -> None:
        self.refs.append(ref)
        self.destinations.append(dest)
        if self.fail:
            raise RuntimeError("archive download failed")
        shutil.copytree(self.source, dest, dirs_exist_ok=True)


class FakeArchiveResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        yield self.payload


class FakeArchiveHTTP:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def stream(self, method: str, url: str, *, follow_redirects: bool = False):
        assert method == "GET"
        assert follow_redirects is True
        self.urls.append(url)
        return FakeArchiveResponse(self.payload)


def _pr() -> PRContext:
    return PRContext(
        repo="o/r",
        pr_number=7,
        head_sha="sha-abc",
        base_sha="sha-base",
        title="workspace",
        author="dev",
        html_url="",
        diff_url="",
    )


def test_detect_verify_command_prefers_configured_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.code_review_pipeline.workspace import detect_verify_command

    monkeypatch.setattr(
        settings,
        "code_review_verify_command",
        "python -m pytest -q",
        raising=False,
    )
    assert detect_verify_command(tmp_path) == ("python", "-m", "pytest", "-q")


def test_detect_verify_command_uses_pytest_for_python_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.code_review_pipeline.workspace import detect_verify_command

    monkeypatch.setattr(settings, "code_review_verify_command", "", raising=False)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()

    assert detect_verify_command(tmp_path) == (sys.executable, "-m", "pytest", "-q")


@pytest.mark.asyncio
async def test_provisioner_downloads_and_cleans_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.code_review_pipeline.workspace import GitHubArchiveWorkspaceProvisioner

    monkeypatch.setattr(settings, "code_review_workspace_root", str(tmp_path / "roots"), raising=False)
    source = tmp_path / "source"
    (source / "app").mkdir(parents=True)
    (source / "app" / "calc.py").write_text("value = 1\n", encoding="utf-8")
    client = CopyingArchiveClient(source)
    provisioner = GitHubArchiveWorkspaceProvisioner(
        client,  # type: ignore[arg-type]
        verify_command=(sys.executable, "-c", "print('ok')"),
    )

    workspace = await provisioner.prepare(_pr())

    assert workspace.error == ""
    assert workspace.path is not None
    assert (workspace.path / "app" / "calc.py").read_text(encoding="utf-8") == "value = 1\n"
    assert client.refs == ["sha-abc"]
    await provisioner.cleanup(workspace)
    assert not workspace.path.exists()


@pytest.mark.asyncio
async def test_provisioner_returns_explicit_error_when_download_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.code_review_pipeline.workspace import GitHubArchiveWorkspaceProvisioner

    monkeypatch.setattr(settings, "code_review_workspace_root", str(tmp_path / "roots"), raising=False)
    client = CopyingArchiveClient(tmp_path, fail=True)
    provisioner = GitHubArchiveWorkspaceProvisioner(
        client,  # type: ignore[arg-type]
        verify_command=(sys.executable, "-c", "print('ok')"),
    )

    workspace = await provisioner.prepare(_pr())

    assert workspace.path is None
    assert "archive download failed" in workspace.error


def test_extract_repo_tarball_strips_the_github_top_level(tmp_path: Path) -> None:
    from apps.code_review_pipeline.workspace import extract_repo_tarball

    source = tmp_path / "source"
    (source / "repo-main" / "tests").mkdir(parents=True)
    (source / "repo-main" / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert True\n",
        encoding="utf-8",
    )
    archive = tmp_path / "repo.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source / "repo-main", arcname="repo-main")

    dest = tmp_path / "dest"
    extract_repo_tarball(archive, dest)

    assert (dest / "tests" / "test_x.py").is_file()


@pytest.mark.asyncio
async def test_github_client_downloads_and_extracts_archive(tmp_path: Path) -> None:
    from apps.code_review_pipeline.routing.github_client import GitHubClient, GitHubRepo

    source = tmp_path / "source"
    (source / "repo-sha" / "app").mkdir(parents=True)
    (source / "repo-sha" / "app" / "calc.py").write_text("value = 1\n", encoding="utf-8")
    archive = tmp_path / "repo.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source / "repo-sha", arcname="repo-sha")

    fake_http = FakeArchiveHTTP(archive.read_bytes())
    client = GitHubClient(token="t")
    await client._client.aclose()  # noqa: SLF001
    client._client = fake_http  # type: ignore[assignment]  # noqa: SLF001

    dest = tmp_path / "dest"
    await client.download_archive(GitHubRepo(owner="o", name="r"), "sha-abc", dest)

    assert fake_http.urls == ["/repos/o/r/tarball/sha-abc"]
    assert (dest / "app" / "calc.py").read_text(encoding="utf-8") == "value = 1\n"
