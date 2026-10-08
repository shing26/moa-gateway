"""合并审批的执行器（ADR-021）。

回调里点击批准/拒绝后，由本执行器完成状态推进与 GitHub 合并动作。
**不走** ``engine.decide_hitl()``：合并审批是独立的写操作，与聊天 FSM 的
会话状态无关。

核心流程：
1. 从 task_key 解析 identity
2. transition(APPROVE/REJECT, approver=operator_id)
3. approve 时：读 get_pr() 检查 mergeable → merge_pr(sha=head_sha) → 映射结果
4. 写审计链
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from apps.code_review_pipeline.merge_state import MergeAction
from apps.code_review_pipeline.merge_store import (
    MergeNotFound,
    MergeRecord,
    MergeStore,
)
from apps.code_review_pipeline.routing.github_client import (
    GitHubRepo,
    MergeOutcome,
)

logger = logging.getLogger("moa.merge_executor")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_task_key(task_key: str) -> tuple[str, int, str]:
    """``owner/repo#123@sha`` → ``(repo, pr_number, head_sha)``。"""
    repo, _, rest = task_key.partition("#")
    pr_str, _, sha = rest.partition("@")
    return (repo, int(pr_str), sha)


class MergeExecutor:
    def __init__(self, store: MergeStore, github: Any) -> None:
        self._store = store
        self._github = github

    async def execute(
        self, task_key: str, action: str, approver: str
    ) -> MergeOutcome:
        """执行合并审批结果。

        ``action`` 是 ``"approve"`` 或 ``"reject"``。
        返回 ``MergeOutcome`` 供调用方判断后续动作（发通知、写审计）。
        """
        identity = _parse_task_key(task_key)
        record = self._store.get(identity)
        if record is None:
            raise MergeNotFound(f"no merge record for {task_key!r}")

        if action == "reject":
            self._store.transition(
                identity,
                MergeAction.REJECT,
                at=_now_iso(),
                approver=approver,
            )
            logger.info("merge rejected by %s: %s", approver, task_key)
            return MergeOutcome(merged=False, status="rejected", message="已拒绝")

        # approve 路径
        self._store.transition(
            identity,
            MergeAction.APPROVE,
            at=_now_iso(),
            approver=approver,
        )

        repo = GitHubRepo.from_name(record.repo) if hasattr(GitHubRepo, 'from_name') else _repo_from_name(record.repo)
        pr_number = record.pr_number

        # 合并前检查可合并性（dirty 走失败路径，不尝试合并）
        try:
            pr_info = await self._github.get_pr(repo, pr_number)
        except Exception as exc:
            logger.warning("get_pr failed for %s: %s", task_key, exc)
            self._store.transition(
                identity,
                MergeAction.FAIL,
                at=_now_iso(),
                failure_reason=f"get_pr failed: {exc}",
            )
            return MergeOutcome(merged=False, status="error", message=str(exc))

        mergeable = pr_info.get("mergeable")
        mergeable_state = pr_info.get("mergeable_state", "")
        if mergeable is False or mergeable_state == "dirty":
            reason = f"PR is not mergeable (mergeable={mergeable}, state={mergeable_state})"
            logger.warning("merge blocked for %s: %s", task_key, reason)
            self._store.transition(
                identity,
                MergeAction.FAIL,
                at=_now_iso(),
                failure_reason=reason,
            )
            return MergeOutcome(merged=False, status="not_mergeable", message=reason)

        # 执行合并（sha 即乐观锁）
        outcome = await self._github.merge_pr(repo, pr_number, sha=record.head_sha)
        if outcome.merged:
            self._store.transition(
                identity,
                MergeAction.COMPLETE,
                at=_now_iso(),
                merged_sha=outcome.sha,
            )
            logger.info("merge completed for %s: %s", task_key, outcome.sha)
        else:
            self._store.transition(
                identity,
                MergeAction.FAIL,
                at=_now_iso(),
                failure_reason=outcome.message,
            )
            logger.warning("merge failed for %s: %s", task_key, outcome.message)

        return outcome


def _repo_from_name(repo: str) -> GitHubRepo:
    """``owner/repo`` → ``GitHubRepo``。"""
    owner, _, name = repo.partition("/")
    return GitHubRepo(owner=owner, name=name)
