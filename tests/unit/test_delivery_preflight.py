"""交付开工前自检。

回归（2026-11-05，issue #43 交付）：两次运行把实现与验证都做完了，才在推送时
撞上 403——token 的 Contents 是 write，但改 `.github/workflows/` 需要 Workflows
写权限。诊断落在最贵的一步之后。这里钉住"开工前就说得清"的那几条。
"""

from __future__ import annotations

import httpx
import pytest

from apps.code_review_pipeline.delivery.preflight import (
    STATUS_BLOCKED,
    STATUS_OK,
    STATUS_WARN,
    run_preflight,
)
from apps.code_review_pipeline.routing.github_client import (
    GITHUB_API,
    GitHubClient,
)

REPO = "o/r"
BASE_SHA = "a" * 40


class _FakePostgresStore:
    """只实现自检用到的那一个读操作；类型检查由 monkeypatch 满足。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def list_open(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("connection timeout expired")
        return []


def _client(handler) -> GitHubClient:
    client = GitHubClient(token="t")
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=GITHUB_API,
        headers={"Authorization": "Bearer t"},
        transport=httpx.MockTransport(handler),
    )
    return client


def _ok_handler(*, permissions_push: bool = True, tree_status: int = 201, tree_body=None):
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user":
            return httpx.Response(200, json={"login": "shing26"})
        if path == "/repos/o/r":
            return httpx.Response(
                200,
                json={
                    "permission": "admin" if permissions_push else "read",
                    "permissions": {"push": permissions_push, "pull": True},
                },
            )
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": BASE_SHA}})
        if path.endswith(f"/git/commits/{BASE_SHA}"):
            return httpx.Response(200, json={"tree": {"sha": "t" * 40}})
        if path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "b" * 40})
        if path.endswith("/git/trees"):
            return httpx.Response(
                tree_status,
                json=tree_body
                or {"sha": "c" * 40, "message": "Resource not accessible by personal access token"},
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    return handler


@pytest.fixture
def fake_store(monkeypatch):
    monkeypatch.setattr(
        "apps.code_review_pipeline.merge_store.PostgresMergeStore", _FakePostgresStore
    )
    return _FakePostgresStore


@pytest.mark.asyncio
async def test_all_capabilities_present(fake_store) -> None:
    store = fake_store()

    report = await run_preflight(REPO, github=_client(_ok_handler()), merge_store=store)

    statuses = {check.name: check.status for check in report.checks}
    assert report.ok is True
    assert statuses == {
        "token": STATUS_OK,
        "push": STATUS_OK,
        "branch": STATUS_OK,
        "workflows": STATUS_WARN,  # 未探测：没有证据，但不是阻断
        "merge_store": STATUS_OK,
    }
    assert store.calls == 1


@pytest.mark.asyncio
async def test_read_only_token_blocks_before_implementing(fake_store) -> None:
    report = await run_preflight(
        REPO,
        github=_client(_ok_handler(permissions_push=False)),
        merge_store=fake_store(),
    )

    push = next(check for check in report.checks if check.name == "push")
    assert push.status == STATUS_BLOCKED
    assert "permission=read" in push.detail
    assert report.ok is False


@pytest.mark.asyncio
async def test_workflow_probe_names_the_missing_permission(fake_store) -> None:
    report = await run_preflight(
        REPO,
        github=_client(_ok_handler(tree_status=403)),
        merge_store=fake_store(),
        probe_workflows=True,
    )

    workflows = next(check for check in report.checks if check.name == "workflows")
    assert workflows.status == STATUS_BLOCKED
    assert "Workflows: Read and write" in workflows.detail
    assert report.ok is False


@pytest.mark.asyncio
async def test_workflow_probe_passes_when_tree_is_accepted(fake_store) -> None:
    report = await run_preflight(
        REPO,
        github=_client(_ok_handler(tree_status=201)),
        merge_store=fake_store(),
        probe_workflows=True,
    )

    workflows = next(check for check in report.checks if check.name == "workflows")
    assert workflows.status == STATUS_OK
    assert report.ok is True


@pytest.mark.asyncio
async def test_missing_base_branch_blocks(fake_store) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user":
            return httpx.Response(200, json={"login": "shing26"})
        if path == "/repos/o/r":
            return httpx.Response(200, json={"permission": "admin"})
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(404, json={"message": "Not Found"})
        raise AssertionError(f"unexpected request: {path}")

    report = await run_preflight(
        REPO, github=_client(handler), merge_store=fake_store()
    )

    branch = next(check for check in report.checks if check.name == "branch")
    assert branch.status == STATUS_BLOCKED
    assert "main" in branch.detail


@pytest.mark.asyncio
async def test_unreachable_merge_store_blocks(fake_store) -> None:
    report = await run_preflight(
        REPO,
        github=_client(_ok_handler()),
        merge_store=fake_store(fail=True),
    )

    merge = next(check for check in report.checks if check.name == "merge_store")
    assert merge.status == STATUS_BLOCKED
    assert "connection timeout" in merge.detail


@pytest.mark.asyncio
async def test_bad_repo_never_touches_the_network(fake_store) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be used")

    report = await run_preflight(
        "not-a-repo", github=_client(handler), merge_store=fake_store()
    )

    assert report.ok is False
    assert report.checks[0].name == "repo"
    assert "owner/name" in report.checks[0].detail
