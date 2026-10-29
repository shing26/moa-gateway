"""人工审批 -> 写回 GitHub（D4）。

把三件事绑在一个函数里，而不是让调用方自己依次调：状态机推进、审计留痕、
幂等写回。分开写的话，任何一次重构都可能只做其中一件——而漏掉审计不会有任何
报错，只会让"谁批准了这条结论"永久查不到。

审计与写回的**顺序**也有讲究：先写审计再写回。反过来的话，崩在中间会留下一条
"已批准"的状态但没有审计行，等于这次人工决策在记录里凭空消失。
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from apps.code_review_pipeline.review_publisher import PublishOutcome, publish_review
from apps.code_review_pipeline.task_audit import record_human_decision, record_lifecycle

logger = logging.getLogger("moa.code_review.approval")


class FeedbackIncomplete(ValueError):
    """Raised when a publish is attempted before every finding is labelled."""


class PublishInProgress(RuntimeError):
    """Raised when another process owns the GitHub publish lease."""


async def approve_task(
    store: Any,
    github: Any,
    identity: tuple[str, int, str],
    task_id: str,
    *,
    operator: str,
    summary: str = "",
    dry_run: bool = False,
    findings: tuple[dict[str, Any], ...] = (),
    waited_ms: float = 0.0,
    require_feedback: bool = False,
    expected_review_run_id: str | None = None,
) -> PublishOutcome:
    """批准并写回。返回写回结果（已写回过时会告诉你是哪一条）。"""
    repo = identity[0]

    # 幂等短路（2026-10-02 golden path 实测）。放在状态迁移**之前**，因为已写回的
    # 任务处于终态 done，而 done 不允许任何动作 —— 不短路的话，第二次批准抛的是
    # InvalidTaskTransition，退出码非 0、栈打到终端上，读起来像"底座崩了"。
    #
    # 判据本身没有新增：仍是 D4 那个主判据 `posted_review_id IS NULL`，只是提前一步
    # 问，避免拿一个注定被拒的动作去撞状态机。
    recorded = store.get_posted_review_id(identity)
    if recorded:
        # The review may have been accepted before the process died between
        # ``set_posted_review_id`` and ``complete``. Route this through the
        # publisher's repair branch instead of returning early and leaving the
        # task stranded in ``posting``.
        if not dry_run and getattr(store, "get_task_state", lambda _identity: None)(
            identity
        ) == "posting":
            return await publish_review(
                github,
                store,
                identity,
                task_id,
                summary,
                dry_run=False,
                findings=findings,
                expected_review_run_id=expected_review_run_id,
            )
        logger.info("task %s already has review %s; nothing to approve", task_id, recorded)
        return PublishOutcome(status="already_posted", review_id=str(recorded))

    if require_feedback and hasattr(store, "feedback_status"):
        status = store.feedback_status(task_id)
        if not status.complete:
            raise FeedbackIncomplete(
                f"finding feedback incomplete: {status.labelled_findings}/{status.total_findings}"
            )

    if dry_run:
        # **dry-run 必须零副作用，这一条比"发不发评论"更要紧。**
        #
        # D4 的实现是先 approve->posting、再 publish(dry_run=True)，于是 dry-run 把
        # 任务留在 posting。而 posting 只允许 complete/fail（见 task_state），
        # 于是"先试跑再真跑"这条最自然的路径**永久卡死**——golden path 里第一次
        # 跑 `demo approve` 再跑 `demo approve --real` 就是这么炸的。
        #
        # 推而广之：dry-run 的语义是"预演"，凡是预演里推进的状态，人就没法在真跑
        # 时重新推进。所以这里连状态、连审计都不写，只走一遍 publish 的判定路径
        # （主判据 -> marker 兜底 -> 停在最后一步）。
        outcome = await publish_review(
            github,
            store,
            identity,
            task_id,
            summary,
            dry_run=True,
            findings=findings,
            expected_review_run_id=expected_review_run_id,
        )
        logger.info("task %s dry-run by %s -> %s", task_id, operator, outcome.status)
        return outcome

    # A previous approval may have moved the task to ``posting`` before the
    # GitHub request failed. That state has no posted review id, so retrying is
    # safe: publish_review still checks the marker and can adopt an accepted
    # review before creating another one.
    current_state = getattr(store, "get_task_state", lambda _identity: None)(identity)
    owner = uuid.uuid4().hex
    lease_seconds = int(os.getenv("CODE_REVIEW_PUBLISH_LEASE_SECONDS", "120"))

    def _claim() -> None:
        claim = getattr(store, "claim_publish_attempt", None)
        if claim is None:
            return
        if not claim(
            identity,
            owner=owner,
            lease_seconds=lease_seconds,
            expected_review_run_id=expected_review_run_id,
        ):
            raise PublishInProgress(
                "another publisher owns the active GitHub write lease"
            )

    def _release() -> None:
        release = getattr(store, "release_publish_attempt", None)
        if release is not None:
            release(identity, owner=owner)

    if current_state == "posting":
        _claim()
        # Do not release in a finally block on an ambiguous network failure:
        # another process must wait for the lease to expire before retrying.
        outcome = await publish_review(
            github,
            store,
            identity,
            task_id,
            summary,
            dry_run=False,
            findings=findings,
            expected_review_run_id=expected_review_run_id,
        )
        _release()
        logger.info("task %s retried by %s -> %s", task_id, operator, outcome.status)
        return outcome

    # 1) 原子抢占审批动作。并发回调只有一个能从 waiting_approval 进入
    # posting；失败者不会留下一条虚假的“已批准”外部审计。
    store.transition_task(
        identity,
        "approve",
        expected_trace_id=task_id,
        require_current_trace=True,
        expected_review_run_id=expected_review_run_id,
        metadata={"operator": operator, "decision": "approve"},
    )
    _claim()

    # 2) The operator is also on the authoritative transition row, but the
    # append-only audit chain is still required by ADR-021. If it cannot be
    # written, fail before making any GitHub side effect.
    try:
        await record_human_decision(
            task_id,
            repo,
            operator=operator,
            decision="approve",
            duration_ms=waited_ms,
        )
        await record_lifecycle(task_id, repo, "posting", operator=operator)
    except Exception:
        logger.exception("approval audit failed; refusing GitHub write task=%s", task_id)
        try:
            store.transition_task(
                identity,
                "fail",
                expected_trace_id=task_id,
                require_current_trace=True,
                expected_review_run_id=expected_review_run_id,
                metadata={"reason": "approval_audit_failed"},
            )
        except Exception:
            logger.exception("could not mark task failed after audit error task=%s", task_id)
        _release()
        raise

    # 3) 幂等写回。publish_review 内部会在成功后 complete。
    outcome = await publish_review(
        github,
        store,
        identity,
        task_id,
        summary,
        dry_run=dry_run,
        findings=findings,
        expected_review_run_id=expected_review_run_id,
    )
    _release()
    logger.info("task %s approved by %s -> %s", task_id, operator, outcome.status)
    return outcome


async def reject_task(
    store: Any,
    identity: tuple[str, int, str],
    task_id: str,
    *,
    operator: str,
    reason: str = "",
    waited_ms: float = 0.0,
    expected_review_run_id: str | None = None,
) -> str:
    """人工拒绝。终态是 rejected，**不写回** GitHub。

    拒绝后不写任何评论是刻意的：既然人已经明确否掉了，再去 PR 上留一条"我被
    拒绝了"的消息既没意义又会制造噪声。终态与 done 分开正是为了这个——done 的
    幂等判断是 posted_review_id 非空，而 rejected 天然不会有。
    """
    repo = identity[0]
    store.transition_task(
        identity,
        "reject",
        expected_trace_id=task_id,
        require_current_trace=True,
        expected_review_run_id=expected_review_run_id,
        metadata={
            "operator": operator,
            "decision": "reject",
            "reason": reason,
        },
    )
    try:
        await record_human_decision(
            task_id,
            repo,
            operator=operator,
            decision=f"reject: {reason}" if reason else "reject",
            duration_ms=waited_ms,
        )
        await record_lifecycle(task_id, repo, "rejected", operator=operator, reason=reason)
    except Exception:
        logger.exception("rejection state persisted but audit append failed task=%s", task_id)
    return "rejected"
