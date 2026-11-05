"""Git Data API 写失败时必须带上 GitHub 的原话。

回归（2026-11-05，issue #43 交付第 3 次尝试）：推送在 ``POST /git/trees``
收到 403，异常里只有 URL。凭 403 这三个数字分不清是权限、速率限制还是别的，
只能事后手工复现。这里的断言就是"下次一眼能读出来"。
"""

from __future__ import annotations

import httpx
import pytest

from apps.code_review_pipeline.routing.github_client import (
    GITHUB_API,
    GitHubClient,
    GitHubRepo,
)

REPO = GitHubRepo("o", "r")


def _client(handler) -> GitHubClient:
    client = GitHubClient(token="t")
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=GITHUB_API,
        headers={"Authorization": "Bearer t"},
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_create_commit_surfaces_the_github_message() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/git/commits/base123"):
            return httpx.Response(200, json={"tree": {"sha": "tree123"}})
        if request.url.path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blob123"})
        if request.url.path.endswith("/git/trees"):
            return httpx.Response(
                403,
                json={
                    "message": (
                        "You have exceeded a secondary rate limit. "
                        "Please wait a few minutes before you try again."
                    )
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with pytest.raises(RuntimeError) as excinfo:
        await _client(handler).create_commit(
            REPO,
            base_sha="base123",
            branch="delivery/one",
            message="m",
            files=[{"path": "README.md", "content_b64": "aGk="}],
        )

    message = str(excinfo.value)
    assert "创建 tree" in message
    assert "403" in message
    assert "secondary rate limit" in message


@pytest.mark.asyncio
async def test_create_ref_surfaces_the_github_message() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible"})

    with pytest.raises(RuntimeError, match="Resource not accessible"):
        await _client(handler).create_ref(REPO, "delivery/one", "base123")


@pytest.mark.asyncio
async def test_workflow_file_403_names_the_missing_permission() -> None:
    """改 workflow 文件被 403 时，要说清缺的是 Workflows: write。

    回归（2026-11-05）：token 的 Contents 是 write、blob 也建得出来，唯独
    ``.github/workflows/ci.yml`` 一进 tree 就 403 "Resource not accessible by
    personal access token"。GitHub 不说缺哪一项，于是交付连撞两次。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/git/commits/base123"):
            return httpx.Response(200, json={"tree": {"sha": "tree123"}})
        if request.url.path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blob123"})
        return httpx.Response(
            403, json={"message": "Resource not accessible by personal access token"}
        )

    with pytest.raises(RuntimeError) as excinfo:
        await _client(handler).create_commit(
            REPO,
            base_sha="base123",
            branch="delivery/one",
            message="m",
            files=[{"path": ".github/workflows/ci.yml", "content_b64": "aGk="}],
        )

    message = str(excinfo.value)
    assert "Resource not accessible" in message
    assert "Workflows: Read and write" in message


@pytest.mark.asyncio
async def test_non_workflow_403_does_not_guess_workflow_scope() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/git/commits/base123"):
            return httpx.Response(200, json={"tree": {"sha": "tree123"}})
        if request.url.path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blob123"})
        return httpx.Response(403, json={"message": "Resource not accessible"})

    with pytest.raises(RuntimeError) as excinfo:
        await _client(handler).create_commit(
            REPO,
            base_sha="base123",
            branch="delivery/one",
            message="m",
            files=[{"path": "README.md", "content_b64": "aGk="}],
        )

    assert "Workflows: Read and write" not in str(excinfo.value)
