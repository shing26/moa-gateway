"""写回 GitHub 与幂等判据（D4）。

幂等的**主判据**是任务行的 ``posted_review_id IS NULL``；**兜底**是 review body
里的 ``<!-- moa-task: TASK_ID -->`` marker。两者的分工不是冗余：

- 主判据能覆盖绝大多数情况（我们自己记了 id）。
- 但崩在"GitHub 已接受、id 还没落库"这个窗口时，主判据说"没写过"，而 GitHub 上
  **已经有**那条评论了。此时若直接再发一次，PR 上就会出现两条一模一样的评论——
  而这个窗口无法通过"先查后写"消除（它就是个真实的分布式提交窗口）。
  marker 兜底把窗口补上。

所以顺序必须严格是：先信主判据，再扫 marker，**最后**才发。
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.code_review_pipeline.review_publisher import (
    TASK_MARKER_PREFIX,
    build_review_body,
    find_marked_review,
    marker_for,
    publish_review,
)
from apps.code_review_pipeline.routing.github_client import GitHubRepo

TASK_ID = "cr_o/r#42@abc1234"
IDENTITY = ("o/r", 42, "abc1234")


class FakeGitHub:
    """记录调用的假客户端，只实现 publisher 用到的两个方法。"""

    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.existing = existing or []
        self.posted: list[dict[str, Any]] = []
        self._next_id = 900

    async def list_reviews(self, repo: GitHubRepo, pr_number: int) -> list[dict[str, Any]]:
        return list(self.existing)

    async def create_review(
        self, repo: GitHubRepo, pr_number: int, body: str, *, event: str = "COMMENT"
    ) -> dict[str, Any]:
        self.posted.append(
            {"repo": f"{repo.owner}/{repo.name}", "pr": pr_number, "body": body, "event": event}
        )
        self._next_id += 1
        return {"id": self._next_id}


class FakeStore:
    def __init__(self, posted_review_id: str | None = None) -> None:
        self.posted_review_id = posted_review_id
        self.saved: list[str] = []
        self.transitions: list[str] = []

    def get_posted_review_id(self, identity: tuple[str, int, str]) -> str | None:
        return self.posted_review_id

    def set_posted_review_id(self, identity: tuple[str, int, str], review_id: str) -> None:
        self.posted_review_id = review_id
        self.saved.append(review_id)

    def transition_task(self, identity: tuple[str, int, str], action: str) -> str:
        self.transitions.append(action)
        return action


def test_marker_is_stable_and_prefix_is_exact() -> None:
    """marker 必须能原样嵌进 markdown 注释里，且只由 task_id 决定。"""
    assert marker_for(TASK_ID) == f"<!-- moa-task: {TASK_ID} -->"
    assert marker_for(TASK_ID) == marker_for(TASK_ID)
    assert TASK_MARKER_PREFIX == "<!-- moa-task:"


@pytest.mark.asyncio
async def test_first_publish_posts_and_records_id() -> None:
    gh = FakeGitHub()
    store = FakeStore()
    outcome = await publish_review(gh, store, IDENTITY, TASK_ID, "looks fine", dry_run=False)

    assert outcome.status == "posted"
    assert len(gh.posted) == 1
    assert store.saved == [outcome.review_id]
    assert store.transitions == ["complete"]


@pytest.mark.asyncio
async def test_second_publish_is_a_noop_using_primary_criterion() -> None:
    """主判据命中时**根本不调 GitHub**：连 list_reviews 都不该发。"""
    gh = FakeGitHub()
    store = FakeStore(posted_review_id="12345")
    outcome = await publish_review(gh, store, IDENTITY, TASK_ID, "looks fine", dry_run=False)

    assert outcome.status == "already_posted"
    assert outcome.review_id == "12345"
    assert gh.posted == []


@pytest.mark.asyncio
async def test_marker_backfills_the_crash_window() -> None:
    """兜底：主判据为空但 GitHub 上已有 marker 评论 -> 认领它的 id，不重复发。

    这正是"GitHub 已接受、id 未落库"那个窗口。修复方式不是重发，而是把已存在的
    那条认领下来并补记 id——否则 PR 上会出现两条一样的评论。
    """
    gh = FakeGitHub(
        existing=[
            {"id": 777, "body": "unrelated review"},
            {"id": 888, "body": f"findings\n\n{marker_for(TASK_ID)}"},
        ]
    )
    store = FakeStore()
    outcome = await publish_review(gh, store, IDENTITY, TASK_ID, "findings", dry_run=False)

    assert outcome.status == "adopted"
    assert outcome.review_id == "888"
    assert gh.posted == [], "兜底命中了却还发了一条，PR 上就有重复评论了"
    assert store.saved == ["888"]


@pytest.mark.asyncio
async def test_marker_scan_ignores_other_tasks_comments() -> None:
    """别人的 task marker 不能被认成自己的——否则会漏发真正的评论。"""
    gh = FakeGitHub(existing=[{"id": 111, "body": marker_for("cr_other#1@zzz")}])
    store = FakeStore()
    outcome = await publish_review(gh, store, IDENTITY, TASK_ID, "findings", dry_run=False)
    assert outcome.status == "posted"
    assert len(gh.posted) == 1


@pytest.mark.asyncio
async def test_dry_run_does_not_post_or_record() -> None:
    """dry-run 停在 posting：既不发也不记 id。

    刻意不记 id：记了就等于"认为已经写回"，下次真跑会被主判据挡掉，
    于是这个任务永远发不出去。dry-run 必须是**完全无副作用**的。
    """
    gh = FakeGitHub()
    store = FakeStore()
    outcome = await publish_review(gh, store, IDENTITY, TASK_ID, "findings", dry_run=True)

    assert outcome.status == "dry_run"
    assert gh.posted == []
    assert store.saved == []
    assert store.transitions == [], "dry-run 不该推进到 done，否则任务被判成已完成"
