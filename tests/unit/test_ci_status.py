"""CI 结论聚合与合并结果映射。

这两件是本通道里唯一能纯离线验证的部分：聚合是纯函数，合并映射是
"HTTP 状态码 -> 可预期结果"的翻译。真正要联网的两件事（去 GitHub 读 check-runs、
真去合并）不在这里验证，也不该在这里假装验证。
"""
from __future__ import annotations

import httpx
import pytest

from apps.code_review_pipeline.routing.github_client import (
    CI_FAILURE,
    CI_NONE,
    CI_PENDING,
    CI_SUCCESS,
    GITHUB_API,
    GitHubClient,
    GitHubRepo,
    aggregate_ci_status,
)


def _run(
    name: str, *, status: str = "completed", conclusion: str = "success"
) -> dict[str, object]:
    return {"name": name, "status": status, "conclusion": conclusion}


class TestAggregate:
    def test_all_success_is_green(self) -> None:
        got = aggregate_ci_status([_run("a"), _run("b")])
        assert got.state == CI_SUCCESS
        assert got.is_green
        assert got.failing == ()

    def test_one_failure_beats_three_green(self) -> None:
        """3 绿 + 1 红必须判红。这是"CI 红了却弹出合并卡片"的防线。"""
        got = aggregate_ci_status(
            [_run("a"), _run("b"), _run("c"), _run("d", conclusion="failure")]
        )
        assert got.state == CI_FAILURE
        assert [c.name for c in got.failing] == ["d"]

    def test_failure_beats_pending(self) -> None:
        """失败优先于进行中：一条已失败不该被一条还在跑的掩盖。"""
        got = aggregate_ci_status(
            [
                _run("slow", status="in_progress", conclusion=""),
                _run("bad", conclusion="failure"),
            ]
        )
        assert got.state == CI_FAILURE

    def test_pending_when_nothing_failed(self) -> None:
        got = aggregate_ci_status(
            [_run("a"), _run("b", status="in_progress", conclusion="")]
        )
        assert got.state == CI_PENDING
        assert [c.name for c in got.waiting] == ["b"]

    def test_no_checks_is_none_not_success(self) -> None:
        """没有 CI 不等于全绿——否则没配测试的仓库会照样弹合并卡片。"""
        assert aggregate_ci_status([]).state == CI_NONE
        assert aggregate_ci_status(None, None).state == CI_NONE
        assert aggregate_ci_status([]).is_green is False

    def test_neutral_and_skipped_are_not_failures(self) -> None:
        got = aggregate_ci_status(
            [_run("a"), _run("n", conclusion="neutral"), _run("s", conclusion="skipped")]
        )
        assert got.state == CI_SUCCESS

    def test_cancelled_and_timed_out_are_failures(self) -> None:
        assert aggregate_ci_status([_run("a", conclusion="cancelled")]).state == CI_FAILURE
        assert aggregate_ci_status([_run("a", conclusion="timed_out")]).state == CI_FAILURE

    def test_commit_status_failure_is_seen(self) -> None:
        """两套 API 都要看：只看 check-runs 会漏掉第三方 status 的红。"""
        got = aggregate_ci_status([_run("a")], {"state": "failure"})
        assert got.state == CI_FAILURE
        assert [c.name for c in got.failing] == ["commit-status"]

    def test_commit_status_pending(self) -> None:
        assert aggregate_ci_status([_run("a")], {"state": "pending"}).state == CI_PENDING

    def test_commit_status_error_is_failure(self) -> None:
        assert aggregate_ci_status([_run("a")], {"state": "error"}).state == CI_FAILURE

    def test_commit_status_alone_can_be_green(self) -> None:
        """仓库只用 status、完全不用 Actions 时，也该能判绿。"""
        assert aggregate_ci_status([], {"state": "success"}).state == CI_SUCCESS

    def test_url_is_kept_for_the_card(self) -> None:
        """红的时候卡片要能给出链接，所以 url 不能在聚合时被丢掉。"""
        got = aggregate_ci_status(
            [
                {
                    "name": "a",
                    "status": "completed",
                    "conclusion": "failure",
                    "details_url": "https://ci.example/run/1",
                }
            ]
        )
        assert got.failing[0].url == "https://ci.example/run/1"


def _client(handler: object) -> GitHubClient:
    """把一个 MockTransport 装进 GitHubClient。

    不 monkeypatch ``__init__``：直接换掉内部的 AsyncClient，这样 ``merge_pr`` 的
    真实代码路径（构造 payload、读状态码、翻译结果）一行都不会被绕过。
    """
    client = GitHubClient(token="t")
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url=GITHUB_API,
        headers={"Authorization": "Bearer t"},
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    return client


class TestMergeOutcomeMapping:
    @pytest.mark.asyncio
    async def test_success_is_merged(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "PUT"
            assert request.url.path == "/repos/o/r/pulls/7/merge"
            return httpx.Response(200, json={"merged": True, "sha": "abc123"})

        out = await _client(handler).merge_pr(GitHubRepo("o", "r"), 7, sha="abc123")
        assert out.merged is True
        assert out.status == "merged"
        assert out.sha == "abc123"

    @pytest.mark.asyncio
    async def test_sha_is_sent_as_optimistic_lock(self) -> None:
        """审批是在某个 sha 上做的。不传 sha 就等于允许合并一个没人审过的版本。"""
        seen: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"merged": True, "sha": "abc123"})

        await _client(handler).merge_pr(GitHubRepo("o", "r"), 7, sha="abc123")
        assert seen["sha"] == "abc123"

    @pytest.mark.asyncio
    async def test_no_merge_method_is_sent(self) -> None:
        """不许替用户决定历史长什么样——合并方式必须由仓库自己配置决定。"""
        seen: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"merged": True, "sha": "s"})

        await _client(handler).merge_pr(GitHubRepo("o", "r"), 7, sha="s")
        assert "merge_method" not in seen

    @pytest.mark.asyncio
    async def test_405_is_not_mergeable_not_an_exception(self) -> None:
        """冲突 / 被保护分支拒绝是可预期结果，不该变成 500。"""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(405, json={"message": "Pull Request is not mergeable"})

        out = await _client(handler).merge_pr(GitHubRepo("o", "r"), 7, sha="s")
        assert out.merged is False
        assert out.status == "not_mergeable"
        assert "not mergeable" in out.message

    @pytest.mark.asyncio
    async def test_409_is_sha_mismatch(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={"message": "Head branch was modified"})

        out = await _client(handler).merge_pr(GitHubRepo("o", "r"), 7, sha="s")
        assert out.merged is False
        assert out.status == "sha_mismatch"

    @pytest.mark.asyncio
    async def test_403_is_forbidden(self) -> None:
        """token 没有合并权限时要如实说，而不是让人去日志里猜。"""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "Resource not accessible"})

        out = await _client(handler).merge_pr(GitHubRepo("o", "r"), 7, sha="s")
        assert out.merged is False
        assert out.status == "forbidden"

    @pytest.mark.asyncio
    async def test_500_still_raises(self) -> None:
        """5xx 不是可预期结果，必须抛出去——吞掉它会把故障说成业务失败。"""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"message": "boom"})

        with pytest.raises(httpx.HTTPStatusError):
            await _client(handler).merge_pr(GitHubRepo("o", "r"), 7, sha="s")
