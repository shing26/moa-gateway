"""GitHub 通道的代理策略：默认直连，可显式打开系统代理。"""
from __future__ import annotations

import json

import pytest

from apps.code_review_pipeline.routing.github_client import GitHubClient


def test_defaults_to_direct_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """不设开关时直连——本机代理做 TLS 拦截会让 GitHub 调用死在证书校验上。"""
    monkeypatch.delenv("CODE_REVIEW_GITHUB_TRUST_ENV", raising=False)
    assert GitHubClient(token="t")._client.trust_env is False  # noqa: SLF001


@pytest.mark.parametrize("raw", ["1", "true", "YES"])
def test_env_switch_reopens_proxy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("CODE_REVIEW_GITHUB_TRUST_ENV", raw)
    assert GitHubClient(token="t")._client.trust_env is True  # noqa: SLF001


def test_explicit_argument_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_GITHUB_TRUST_ENV", "1")
    client = GitHubClient(token="t", trust_env=False)
    assert client._client.trust_env is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_create_pull_request_returns_number_url_and_head_sha() -> None:
    import httpx

    from apps.code_review_pipeline.routing.github_client import GitHubRepo

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/name/pulls"
        return httpx.Response(
            201,
            json={
                "number": 17,
                "html_url": "https://github.com/owner/name/pull/17",
                "head": {"sha": "b" * 40},
            },
        )

    client = GitHubClient(token="t")
    await client._client.aclose()  # noqa: SLF001
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        ref = await client.create_pull_request(
            GitHubRepo(owner="owner", name="name"),
            branch="moa/fix",
            base_branch="main",
            title="fix",
            body="body",
        )
    finally:
        await client.aclose()

    assert ref.number == 17
    assert ref.url.endswith("/pull/17")
    assert ref.head_sha == "b" * 40


@pytest.mark.asyncio
async def test_create_commit_uses_each_file_mode() -> None:
    import httpx

    from apps.code_review_pipeline.routing.github_client import GitHubRepo

    tree_entries: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/git/commits/base"):
            return httpx.Response(200, json={"tree": {"sha": "tree-base"}})
        if request.url.path.endswith("/git/blobs"):
            payload = json.loads(request.content)
            return httpx.Response(
                201,
                json={"sha": f"blob-{payload['content']}"},
            )
        if request.url.path.endswith("/git/trees"):
            payload = json.loads(request.content)
            tree_entries.extend(payload["tree"])
            return httpx.Response(201, json={"sha": "tree-new"})
        if request.url.path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "c" * 40})
        if request.url.path.endswith("/git/refs/heads/moa/fix"):
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = GitHubClient(token="t")
    await client._client.aclose()  # noqa: SLF001
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.create_commit(
            GitHubRepo(owner="owner", name="name"),
            base_sha="base",
            branch="moa/fix",
            message="fix",
            files=[
                {"path": "run.sh", "content_b64": "ZWNobyBvaw==", "mode": "100755"},
                {"path": "link", "content_b64": "dGFyZ2V0", "mode": "120000"},
            ],
        )
    finally:
        await client.aclose()

    assert [entry["mode"] for entry in tree_entries] == ["100755", "120000"]
