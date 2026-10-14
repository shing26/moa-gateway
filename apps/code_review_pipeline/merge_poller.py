"""合并通道的 CI 轮询器（ADR-021）。

独立进程运行，定期读所有非终态合并记录的 CI 结论，推进状态机。
返回两类"刚得出结论、需要通知人"的记录，由调用方（worker）发卡片：

* CI 绿 → 审批卡片；
* CI 红 → 失败信息卡片（失败 job 名 + 链接，ADR-021 验收 2）。

**CI 还没出结论（pending / none）时不动状态**：把 pending 当红是一个已修的缺陷
（2026-10-09），它让真实的 pending 永久停在 ``ci_failed`` 并推失败卡片。

读 CI 失败（网络/权限）→ ``MergeAction.FAIL``，**不静默重试**：
失败是终态，原因如实告诉人，要不要重来由人决定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from apps.code_review_pipeline.merge_state import (
    MergeAction,
    MergeState,
    can,
    should_notify_approval,
    should_notify_ci_failure,
)
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.routing.github_client import (
    CI_FAILURE,
    CI_SUCCESS,
    GitHubRepo,
)

logger = logging.getLogger("moa.merge_poller")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_from_name(repo: str) -> GitHubRepo:
    """``owner/repo`` → ``GitHubRepo``。"""
    owner, _, name = repo.partition("/")
    return GitHubRepo(owner=owner, name=name)


@dataclass(frozen=True)
class PollResult:
    """一轮轮询的产物：两类刚得出结论、需要通知人的记录。

    分成两个字段而不是一个 ``list``，因为"发审批卡片"和"发失败卡片"是两种不同
    的通知，调用方必须能分辨——一个 ``list`` 会让调用方靠 ``state`` 反推，而
    ``ci_failed`` 也可能是历史状态，反推就会重复发。
    """

    approvals: tuple[MergeRecord, ...] = ()
    ci_failures: tuple[MergeRecord, ...] = ()


class MergePoller:
    """轮询合并通道里所有非终态记录的 CI 状态。"""

    def __init__(self, store: MergeStore, github: Any) -> None:
        self._store = store
        self._github = github

    async def poll_once(self) -> PollResult:
        """轮询一次，返回需要发卡片的两类记录。

        读 CI 失败的记录直接推进到 ``FAILED``（终态），不静默重试。
        CI 未出结论（pending / none）的记录**原地不动**，等下一轮。
        """
        approvals: list[MergeRecord] = []
        ci_failures: list[MergeRecord] = []
        for record in self._store.list_open():
            try:
                ci = await self._github.get_ci_status(
                    repo=_repo_from_name(record.repo),
                    ref=record.head_sha,
                )
            except Exception as exc:
                logger.warning(
                    "CI read failed for %s: %s", record.task_key, exc
                )
                self._store.transition(
                    record.identity,
                    MergeAction.FAIL,
                    at=_now_iso(),
                    failure_reason=f"CI read failed: {exc}",
                )
                continue

            # 只有确定的结论才推进状态。pending / none 不是结论：
            # 前者是"还在跑"，后者是"这仓库压根没配 CI"。两者都**不该**被当成红——
            # 那会让记录停在 ci_failed 并给人推一条假的失败通知。
            if ci.state == CI_SUCCESS:
                action = MergeAction.CI_GREEN
            elif ci.state == CI_FAILURE:
                action = MergeAction.CI_RED
            else:
                logger.info(
                    "CI not concluded for %s (%s); staying in %s",
                    record.task_key,
                    ci.state,
                    record.state,
                )
                continue

            current_state = MergeState(record.state)
            if not can(current_state, action):
                # 这个状态已经不需要（或不接受）这个结论：
                #   awaiting_approval + 绿 → 卡片早就发过了，重发是打扰；
                #   merging + 绿/红 → 合并请求已经交给 GitHub，此刻由它决定。
                # **不能直接 transition**：非法转移会抛异常，而它在 poll_once 的
                # try 之外——一条待批记录就会中断整轮轮询，把所有其他 PR 一起饿死
                # （2026-10-09 实测：worker 每 60s 打一次 traceback，别的记录不再推进）。
                logger.info(
                    "CI %s for %s in state %s: no action needed",
                    ci.state,
                    record.task_key,
                    record.state,
                )
                continue
            notify_approval = should_notify_approval(current_state, action)
            notify_failure = should_notify_ci_failure(current_state, action)

            updated = self._store.transition(
                record.identity,
                action,
                at=_now_iso(),
                ci_state=ci.state,
                ci_failing=tuple(
                    {"name": c.name, "url": c.url} for c in ci.failing
                ),
            )
            if notify_approval:
                approvals.append(updated)
                logger.info("CI green for %s, approval card needed", record.task_key)
            if notify_failure:
                ci_failures.append(updated)
                logger.info("CI red for %s, failure card needed", record.task_key)

        return PollResult(approvals=tuple(approvals), ci_failures=tuple(ci_failures))
