"""人工审批 -> 写回 GitHub（D4）。

把三件事绑在一个函数里，而不是让调用方自己依次调：状态机推进、审计留痕、
幂等写回。分开写的话，任何一次重构都可能只做其中一件——而漏掉审计不会有任何
报错，只会让"谁批准了这条结论"永久查不到。

审计与写回的**顺序**也有讲究：先写审计再写回。反过来的话，崩在中间会留下一条
"已批准"的状态但没有审计行，等于这次人工决策在记录里凭空消失。
"""

from __future__ import annotations

import logging
from typing import Any

from apps.code_review_pipeline.review_publisher import PublishOutcome, publish_review
from apps.code_review_pipeline.task_audit import record_human_decision, record_lifecycle

logger = logging.getLogger("moa.code_review.approval")


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
) -> PublishOutcome:
    """批准并写回。返回写回结果（已写回过时会告诉你是哪一条）。"""
    repo = identity[0]

    # 1) 先留审计：这一步崩了，任务还停在 waiting_approval，人可以重来。
    await record_human_decision(
        task_id, repo, operator=operator, decision="approve", duration_ms=waited_ms
    )

    # 2) 推进到 posting。publish_review 内部会在成功后 complete。
    store.transition_task(identity, "approve")
    await record_lifecycle(task_id, repo, "posting", operator=operator)

    # 3) 幂等写回。
    outcome = await publish_review(
        github, store, identity, task_id, summary, dry_run=dry_run, findings=findings
    )
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
) -> str:
    """人工拒绝。终态是 rejected，**不写回** GitHub。

    拒绝后不写任何评论是刻意的：既然人已经明确否掉了，再去 PR 上留一条"我被
    拒绝了"的消息既没意义又会制造噪声。终态与 done 分开正是为了这个——done 的
    幂等判断是 posted_review_id 非空，而 rejected 天然不会有。
    """
    repo = identity[0]
    await record_human_decision(
        task_id,
        repo,
        operator=operator,
        decision=f"reject: {reason}" if reason else "reject",
        duration_ms=waited_ms,
    )
    store.transition_task(identity, "reject")
    await record_lifecycle(task_id, repo, "rejected", operator=operator, reason=reason)
    return "rejected"
