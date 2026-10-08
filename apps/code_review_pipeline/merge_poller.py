"""合并通道的 CI 轮询器（ADR-021）。

独立进程运行，定期读所有非终态合并记录的 CI 结论，推进状态机。
CI 绿时返回需要发审批卡片的记录，由调用方（worker）发卡片。

读 CI 失败（网络/权限）→ ``MergeAction.FAIL``，**不静默重试**：
失败是终态，原因如实告诉人，要不要重来由人决定。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from apps.code_review_pipeline.merge_state import (
    MergeAction,
    MergeState,
    should_notify_approval,
)
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.routing.github_client import GitHubRepo

logger = logging.getLogger("moa.merge_poller")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_from_name(repo: str) -> GitHubRepo:
    """``owner/repo`` → ``GitHubRepo``。"""
    owner, _, name = repo.partition("/")
    return GitHubRepo(owner=owner, name=name)


class MergePoller:
    """轮询合并通道里所有非终态记录的 CI 状态。"""

    def __init__(self, store: MergeStore, github: Any) -> None:
        self._store = store
        self._github = github

    async def poll_once(self) -> list[MergeRecord]:
        """轮询一次，返回需要发审批卡片的记录（CI 绿时）。

        读 CI 失败的记录直接推进到 ``FAILED``（终态），不静默重试。
        """
        results: list[MergeRecord] = []
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

            action = MergeAction.CI_GREEN if ci.is_green else MergeAction.CI_RED
            current_state = MergeState(record.state)
            should_notify = should_notify_approval(current_state, action)

            updated = self._store.transition(
                record.identity,
                action,
                at=_now_iso(),
                ci_state=ci.state,
                ci_failing=tuple(
                    {"name": c.name, "url": c.url} for c in ci.failing
                ),
            )
            if should_notify:
                results.append(updated)
                logger.info("CI green for %s, approval card needed", record.task_key)

        return results
