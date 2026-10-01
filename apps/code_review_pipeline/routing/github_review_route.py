from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.deps import tracer
from app.middleware.auth import insecure_mode_enabled
from apps.code_review_pipeline.storage.review_store import ReviewStore, build_review_store
from apps.code_review_pipeline.task_queue import TaskQueue
from apps.code_review_pipeline.routing.github_signature import verify_signature
from apps.code_review_pipeline.routing.github_provider import github_configured

github_review_router = APIRouter()
logger = logging.getLogger("moa.code_review.route")

# 惰性单例：**import 阶段不得连数据库**（2026-10-01）。
#
# 此前是模块级 `_review_store = build_review_store()`，而 build 会真的执行建表 DDL。
# 于是 `import app.main` / pytest 收集 / `python -m app.cli` 这些只想看一眼的诊断
# 动作，全都附赠一次真实 PostgreSQL 连接。库不可达时（compose 没起）表现为**进程
# 挂死**且零日志 —— 实测挂 >2.5 分钟。对照 app/vectordb/pgvector_client.py：那边
# 连接是 lazy 的，import 不碰网络。这里此前是同一条链上唯一在 import 期做 I/O 的点。
#
# 仍然需要 import 期就暴露配置错误，所以 `build_review_store` 抛出的 StorageInitError
# 不再被 import 吞掉 —— 它改为在第一次真正写记录时抛出（见 `_get_review_store`），
# 那是能在响应里如实降级为 5xx/日志的位置，而不是让整个进程连启动都完不成。
_review_store: ReviewStore | None = None
_task_queue: TaskQueue | None = None


def _get_review_store() -> ReviewStore:
    global _review_store
    if _review_store is None:
        _review_store = build_review_store()
    return _review_store


def _get_task_queue() -> TaskQueue:
    """惰性构建队列客户端（同样不在 import 期连 Redis，理由见 _review_store）。"""
    global _task_queue
    if _task_queue is None:
        from redis.asyncio import Redis

        _task_queue = TaskQueue(
            Redis.from_url(settings.redis_url, decode_responses=False)
        )
    return _task_queue


# 只有这三个 action 值得审一遍。worker 端把 action 固定成 "synchronize"（队列消息
# 只带身份，语义"这份代码要审一遍"由入口判定），所以**过滤必须发生在这里**——
# 否则 closed / labeled / edited 这类事件会各自变成一次完整的 5-agent 审查。
# 这份清单与 build_pr_context_from_github 的是同一件事，单点声明在这里，
# 那边保留自己的检查是因为它也可能被非 webhook 路径直接调用。
REVIEWABLE_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


def build_task_key(repo: str, pr_number: Any, head_sha: str) -> str:
    """任务的稳定身份：``owner/repo#123@abc1234``。

    **为什么不用 delivery id**：``X-GitHub-Delivery`` 每次重投都不同——那是"这次投递"
    的身份。而重投、重复 @ 机器人、webhook 重试，在人看来都是**同一件事要做一遍**。
    真正代表"这份代码该被审一次"的是 head_sha。

    修复的根因（2026-10-01）：此前 ``trace_id = f"cr_{repo}:{body.get('id','')}"``，
    而 GitHub 的 ``pull_request`` 事件**顶层没有 ``id`` 字段**（PR 身份在
    ``pull_request.id``，投递身份在 ``X-GitHub-Delivery`` 头）。于是同一仓库的所有
    PR 共用一个主键 ``cr_owner/repo:``，而 ``review_store.save`` 是
    ``ON CONFLICT (trace_id) DO UPDATE``——**后一个 PR 直接覆盖前一个的记录**，
    ``code_review_findings`` 也按 trace_id 外键挂在同一条下。既有 e2e 只断言了
    ``"trace_id" in body``、没断言唯一性，所以一直没被测出来。
    """
    # 完整 sha，不截断。此前截断到 12 hex（48 bit）是个**假安全**的优化：它让
    # 读起来短，但两个不同 commit 只要共享前 12 位就得到同一个 key，而 D2 要加的
    # `UNIQUE(repo, pr_number, head_sha)` 拦不住——INSERT 会先在 trace_id 主键上
    # 冲突，走 `DO UPDATE` 覆盖前一条。也就是"加唯一约束"看起来防住了重复，实际
    # 漏在更早的那一环。同类分叉第四次（DSN 读取 / embedding 维度 / trace_id 的
    # body['id'] / 这次的身份截断）：同一个业务身份在两处各写一遍，迟早不一致。
    # 短 key 对人类可读性的收益，远小于"重复投递静默丢数据"的代价。
    sha = (head_sha or "").strip() or "unknown"
    number = int(pr_number or 0)
    return f"{repo}#{number}@{sha}"


def record_from_webhook(task_key: str, body: dict[str, Any]) -> Any:
    """从 **webhook payload** 建任务行（入队时刻的快照，不是审查结论）。

    为什么字段取自 payload 而不是审查结果：D3 之后审查跑在 worker 里，webhook 这一
    刻**还没有任何结论**。入队时要先把身份三列落库——worker 认领时就是按它们查的行，
    晚一步就变成"消息指向不存在的任务"被 ack 掉。

    这也顺手修掉了一个老问题：``_record_from_result`` 只能在审查跑完后调，于是
    "入队时任务行不存在"这个窗口一直存在；只是此前 webhook 同步跑审查，窗口小到
    没人踩到。
    """
    from apps.code_review_pipeline.storage.review_store import ReviewRecord

    pr = dict(body.get("pull_request", {}))
    repo_meta = dict(body.get("repository", {}))
    return ReviewRecord(
        trace_id=f"cr_{task_key}",
        repo=str(repo_meta.get("full_name", "")),
        pr_number=int(pr.get("number", 0) or 0),
        head_sha=str(pr.get("head", {}).get("sha", "")),
        author=str(pr.get("user", {}).get("login", "")),
        # 结论未知：计数为 0、"是否需要人工"为 False。worker 跑完会覆盖这两列。
        findings_count=0,
        need_human_review=False,
        raw={
            "base_sha": str(pr.get("base", {}).get("sha", "")),
            "title": str(pr.get("title", "")),
            "html_url": str(pr.get("html_url", "")),
            "diff_url": str(pr.get("diff_url", "")),
            "changed_files_count": int(pr.get("changed_files", 0) or 0),
            "labels": [
                str(x.get("name", "")) if isinstance(x, dict) else str(x)
                for x in (pr.get("labels") or [])
            ],
            "reviewers": [
                str(x.get("login", "")) if isinstance(x, dict) else str(x)
                for x in (pr.get("requested_reviewers") or [])
            ],
        },
    )


@github_review_router.post("/webhook/github/review")
async def github_review_webhook(request: Request) -> JSONResponse:
    with tracer.start_as_current_span("moa.code_review.webhook") as span:
        # 签名必须对**原始字节**校验：HMAC 覆盖 GitHub 发出的请求体，重新序列化
        # （key 顺序 / 空白 / Unicode 转义）都会让摘要对不上。所以先取 bytes，
        # 再自己解析。
        raw = await request.body()
        ok, reason = verify_signature(
            raw,
            request.headers.get("X-Hub-Signature-256"),
            settings.github_webhook_secret,
            allow_insecure=insecure_mode_enabled(settings.gateway_allow_insecure),
        )
        if not ok:
            logger.warning("github webhook rejected: %s", reason)
            # 响应体只回笼统原因，不回 reason：把"缺头"和"算错"分开告诉调用方，
            # 等于送一个免费的区分 oracle 给攻击者。细节进日志。
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "invalid_json"}, status_code=400)

        repo = str(body.get("repository", {}).get("full_name", ""))
        pr_number = body.get("pull_request", {}).get("number")
        head_sha = str(body.get("pull_request", {}).get("head", {}).get("sha", ""))
        task_key = build_task_key(repo, pr_number, head_sha)
        trace_id = f"cr_{task_key}"
        span.set_attribute("moa.channel", "github")
        span.set_attribute("moa.trace_id", trace_id)
        span.set_attribute("moa.task_key", task_key)

        action = str(body.get("action", "")).lower()
        if action not in REVIEWABLE_ACTIONS:
            # 非审查事件照收不误，但不入队。返回 200 而不是 4xx：GitHub 侧看到
            # 2xx 就不会重投，而"这条事件我们不管"确实不是投递方的错。
            logger.info("ignoring github event action=%s task=%s", action, task_key)
            return JSONResponse({
                "trace_id": trace_id,
                "task_key": task_key,
                "status": "ignored",
                "reason": f"action {action or '(missing)'} is not reviewable",
            }, status_code=200)

        if not github_configured():
            # fail-fast：GitHub 侧不可用时**当场**如实降级。若照常入队，worker 会
            # 在半夜对着一个注定失败的取数请求抛异常，而投递方早就收到 202 走了。
            logger.error("github review skipped: no GitHub credentials configured")
            return JSONResponse({
                "trace_id": trace_id,
                "task_key": task_key,
                "status": "degraded",
                "message": "PR 审查未执行：GITHUB_TOKEN 未配置。",
            }, status_code=200)

        store = _get_review_store()
        identity = (repo, int(pr_number or 0), head_sha)
        # 先看状态再落库：已经审过（含任何终态）就是重投，返回 200 且**不再入队**。
        # 不靠"入队前先查"来做幂等——查与写之间有窗口，而真正的幂等判据是消费端的
        # 条件 UPDATE 认领。这里查状态只为给出正确的 HTTP 语义（202 vs 200）。
        existing = store.get_task_state(identity)
        store.save(record_from_webhook(task_key, body))

        try:
            message_id = await _get_task_queue().enqueue(repo, identity[1], head_sha)
        except Exception:
            logger.exception("could not enqueue review task %s", task_key)
            # 503 而非 202：任务行已经是 queued，但**没有任何东西会去跑它**。说"已
            # 受理"是撒谎。投递方重投是安全的（重投即重新入队，行数不变）。
            return JSONResponse({
                "trace_id": trace_id,
                "task_key": task_key,
                "status": "queue_unavailable",
                "message": "任务队列不可用，请重试投递。",
            }, status_code=503)

        span.set_attribute("moa.queue_message_id", message_id)
        if existing is not None:
            logger.info(
                "redelivery of %s (state=%s); re-enqueued for idempotent skip",
                task_key,
                existing,
            )
            return JSONResponse({
                "trace_id": trace_id,
                "task_key": task_key,
                "repo": repo,
                "pr_number": identity[1],
                "state": existing,
                "status": "idempotent",
                "detail": "该 PR 的这个 commit 已在审查流程中或已完成。",
            }, status_code=200)

        # 202：已受理，未完成。**响应体里没有 findings_by_severity**——那要等 5 个
        # agent 跑完才知道，而返回它就意味着又回到了"webhook 同步跑审查"。
        logger.info("queued review task %s as %s", task_key, message_id)
        return JSONResponse({
            "trace_id": trace_id,
            "task_key": task_key,
            "repo": repo,
            "pr_number": identity[1],
            "state": "queued",
            "status": "accepted",
        }, status_code=202)
