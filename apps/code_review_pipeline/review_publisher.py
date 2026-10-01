"""写回 GitHub PR review，以及 D4 的幂等双保险（D4）。

**为什么必须双保险**：GitHub 的 review API 没有 idempotency key，而"查有没有发过"
和"发"之间存在一个真实的分布式提交窗口——崩在中间时，我们查不到、但 GitHub 上
已经有了。所以：

1. **主判据**：任务行的 ``posted_review_id``。我们自己记了 id 就绝不再发。
2. **兜底**：扫已有 review 的 body，找 ``<!-- moa-task: TASK_ID -->`` marker。
   命中说明上次发出去了只是没记上 id——这时**认领**那条既有评论的 id 并补记，
   而不是重发。顺序不能反：先发再查，窗口就永远补不上了。

``--dry-run``（默认）走完全相同的判定路径，只在最后一步停下。既不 POST、也不记
id、更不推进到 done。**dry-run 必须零副作用**——若它记了 id，下次真跑会被主判据
挡掉，这个任务就永远发不出去了。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("moa.code_review.publisher")

TASK_MARKER_PREFIX = "<!-- moa-task:"


def marker_for(task_id: str) -> str:
    """任务身份 marker。**只由 task_id 决定**——它必须可预测，否则兜底无从匹配。"""
    return f"{TASK_MARKER_PREFIX} {task_id} -->"


@dataclass(frozen=True)
class PublishOutcome:
    """写回结果。``status`` 四取值，别名收敛在这一处。"""

    status: str
    review_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def build_review_body(
    task_id: str,
    summary: str,
    findings: tuple[dict[str, Any], ...] = (),
) -> str:
    """拼 review body。marker 放在**最后**一行。

    放最后是刻意的：marker 是 HTML 注释，正常不会被人看见；而放在开头会让人
    先读到一行莫名其妙的注释。前面必须是**人读的内容**——这条评论是给人看的，
    机器可读只是副产品。
    """
    lines: list[str] = []
    if summary.strip():
        lines.append(summary.strip())
    for item in findings:
        sev = str(item.get("severity", "")).upper()
        loc = str(item.get("file", ""))
        line = item.get("line")
        where = f"{loc}:{line}" if line else loc
        lines.append(f"- **{sev}** `{where}` {item.get('title', '')}".rstrip())
    lines.append("")
    lines.append(marker_for(task_id))
    return "\n".join(lines)


def find_marked_review(reviews: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    """在已有 review 里找出属于本任务的那条。

    两个条件都匹配才算：前缀存在**且**完整 marker 命中。少了后一个条件的话，
    别的任务的 marker 会被当成自己的——于是真正的评论被漏发，而 PR 上还看不出
    哪里不对。宁可多发一次（主判据仍会挡），也不能漏发。
    """
    exact = marker_for(task_id)
    for review in reviews:
        body = str(review.get("body", ""))
        if TASK_MARKER_PREFIX in body and exact in body:
            return review
    return None


async def publish_review(
    github: Any,
    store: Any,
    identity: tuple[str, int, str],
    task_id: str,
    summary: str,
    *,
    dry_run: bool = False,
    findings: tuple[dict[str, Any], ...] = (),
) -> PublishOutcome:
    """按 主判据 → 兜底 marker → 实际 POST 的顺序写回。"""
    from apps.code_review_pipeline.routing.github_client import GitHubRepo

    repo_name, pr_number, _sha = identity
    owner, _, name = repo_name.partition("/")
    repo = GitHubRepo(owner=owner, name=name)

    async def _complete(reason: str) -> None:
        """推进到 done **并留审计**。

        收编成一个函数是有原因的：原先这两处 ``transition_task(..., "complete")``
        是裸调用，状态推进了、审计没写。后果是审计链里看不出任务是怎么收尾的——
        "写回成功"与"在 posting 阶段被杀掉"留下的记录**完全一样**。而这条链
        存在的全部意义就是"事后能证明发生了什么"，少写最后一步等于在最关键的地方
        留白。

        顺序与 approve_task 一致：**先审计后推进**。崩在中间会留下一条 complete
        审计而状态仍在 posting，人能看出来"记录与状态不一致"，反过来则查不出来。
        """
        from apps.code_review_pipeline.task_audit import record_lifecycle

        await record_lifecycle(task_id, repo_name, "complete", reason=reason)
        store.transition_task(identity, "complete")

    # store 的方法是**同步**的（沿用既有 psycopg 同步客户端的约定），只有 GitHub
    # 那一侧 await。两边不一致会让调用点漏 await 或者 await 一个非协程——
    # 后者是 TypeError，报错位置还离真正的原因很远。
    recorded = store.get_posted_review_id(identity)
    if recorded:
        return PublishOutcome(status="already_posted", review_id=str(recorded))

    existing = await github.list_reviews(repo, pr_number)
    adopted = find_marked_review(existing, task_id)
    if adopted is not None:
        found_id = str(adopted.get("id", ""))
        store.set_posted_review_id(identity, found_id)
        await _complete("adopted")
        logger.info("adopted existing review %s for %s", found_id, task_id)
        return PublishOutcome(status="adopted", review_id=found_id)

    if dry_run:
        return PublishOutcome(status="dry_run", detail={"would_post": True})

    body = build_review_body(task_id, summary, findings)
    created = await github.create_review(repo, pr_number, body, event="COMMENT")
    review_id = str(created.get("id", ""))
    store.set_posted_review_id(identity, review_id)
    await _complete("posted")
    return PublishOutcome(status="posted", review_id=review_id)
