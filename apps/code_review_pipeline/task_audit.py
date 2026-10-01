"""任务级审计行（D4）：5 条 agent + 1 条任务 lifecycle + 1 条人工决策。

**为什么 worker 必须自己写**：审计此前只由 HTTP 中间件写，而 PR 审查跑在 worker
进程里、不经过 HTTP。若不补这条路径，5 个 agent 干了什么、任务怎么推进的、
谁按了批准，全都在审计之外——队列里的事查不到。

``trace_id`` 一律用 task_id（``repo#pr@sha``）。这不是"顺便复用"：审计按 trace
聚类，而 task_id 正是这个 PR 的稳定身份。用请求 id 的话，重投一次就变成两条
互不相关的记录，而人看来它们是同一件事。
"""

from __future__ import annotations

import logging
from typing import Any

from app.audit.models import AuditEntry
from app.audit.recorder import record

logger = logging.getLogger("moa.code_review.audit")

# 五个 agent 的**顺序固定**。顺序有意义：审计行按写入顺序串成哈希链，固定顺序
# 才能让两次运行同一任务产生可逐行比对的结果。
AGENT_NAMES = (
    "triage",
    "static_analysis",
    "semantic_review",
    "test_coverage",
    "report",
)

# 非 agent 的两类行用独立 agent_name，与五个 agent 区分开——于是"这个 task_id 下
# 有哪些行"能一眼分清哪些是 agent 输出、哪些是系统事件。
LIFECYCLE_AGENT = "task_lifecycle"
HUMAN_DECISION_AGENT = "human_decision"


def _summarize_section(section: Any) -> tuple[str, int]:
    """把一个 agent 的产出压成 (一句话, findings 数)。

    不把完整 findings 灌进审计：一行几千字会撑爆 jsonl，而审计要回答的是
    "这个 agent 干了什么、结论是什么"，不是替代结果存储。
    """
    findings = list(getattr(section, "findings", ()) or ())
    if not findings:
        return ("no findings", 0)
    by_sev: dict[str, int] = {}
    for f in findings:
        sev = str(getattr(f, "severity", "") or "unknown").lower()
        by_sev[sev] = by_sev.get(sev, 0) + 1
    parts = ", ".join(f"{k}={v}" for k, v in sorted(by_sev.items()))
    return (parts, len(findings))


def _entry(
    task_id: str,
    repo: str,
    agent_name: str,
    output: str,
    intent: str,
    extra: dict[str, Any],
) -> AuditEntry:
    return AuditEntry(
        trace_id=task_id,
        session_id=repo or "unknown",
        agent_name=agent_name,
        agent_output=output,
        intent=intent,
        extra=extra,
    )


async def record_agent_rows(task_id: str, repo: str, result: Any) -> int:
    """为五个 agent 各写一行。返回写入条数。"""
    written = 0
    for name in AGENT_NAMES:
        section = getattr(result, name, None)
        summary, count = _summarize_section(section)
        await record(
            _entry(
                task_id,
                repo,
                name,
                summary,
                "code_review_agent",
                {"agent": name, "findings": count, "task_id": task_id},
            )
        )
        written += 1
    return written


async def record_lifecycle(task_id: str, repo: str, action: str, **detail: Any) -> None:
    """任务状态推进时写一行（如 queued→running、running→waiting_approval）。"""
    await record(
        _entry(
            task_id,
            repo,
            LIFECYCLE_AGENT,
            action,
            "task_lifecycle",
            {"task_id": task_id, "action": action, **detail},
        )
    )


async def record_human_decision(
    task_id: str,
    repo: str,
    *,
    operator: str,
    decision: str,
    duration_ms: float = 0.0,
) -> None:
    """人工批准/拒绝写一行——这是"谁让这条 agent 结论生效"的唯一记录。"""
    await record(
        _entry(
            task_id,
            repo,
            HUMAN_DECISION_AGENT,
            decision,
            "human_decision",
            {
                "task_id": task_id,
                "operator": operator,
                "decision": decision,
                "hitl_duration_ms": duration_ms,
            },
        )
    )
