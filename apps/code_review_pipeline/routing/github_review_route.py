from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.deps import logger, tracer
from app.middleware.auth import insecure_mode_enabled
from app.models.events import MoAEvent, PlatformEvent
from apps.code_review_pipeline.reporting import build_notification, count_by_severity
from apps.code_review_pipeline.agents.code_review_pipeline import CodeReviewPipeline
from apps.code_review_pipeline.notifications.feishu_notifier import FeishuReviewNotifier
from apps.code_review_pipeline.storage.review_store import ReviewStore, build_review_store
from apps.code_review_pipeline.routing.github_signature import verify_signature

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
_feishu_notifier = FeishuReviewNotifier.from_env()


def _get_review_store() -> ReviewStore:
    global _review_store
    if _review_store is None:
        _review_store = build_review_store()
    return _review_store


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
        platform_event = PlatformEvent(
            platform="github",
            message_id=task_key,
            session_id=repo,
            user_id=str(body.get("pull_request", {}).get("user", {}).get("login", "")),
            payload=body,
        )
        trace_id = f"cr_{task_key}"
        span.set_attribute("moa.channel", "github")
        span.set_attribute("moa.trace_id", trace_id)
        span.set_attribute("moa.task_key", task_key)

        try:
            pipeline = CodeReviewPipeline.from_env()
        except Exception as exc:
            logger.error("github review pipeline init failed: %s", exc)
            if "GITHUB_TOKEN" in str(exc):
                message = "PR 审查未执行：GITHUB_TOKEN 未配置。"
            else:
                message = "PR 审查未执行：审查流水线未配置完成。"
            return JSONResponse({
                "trace_id": trace_id,
                "status": "degraded",
                "message": message,
            }, status_code=200)

        event = MoAEvent(
            trace_id=trace_id,
            event=None,
            session_id=platform_event.session_id,
            text="",
            context=body,
        )

        try:
            pr, result = await pipeline.run(event)
        except Exception as exc:
            logger.exception("github review run failed")
            return JSONResponse({
                "error": "pipeline_run_failed",
                "detail": "PR 审查执行失败，请稍后重试。",
            }, status_code=500)

        _get_review_store().save(_record_from_result(result))

        findings_by_severity = count_by_severity(result)
        notification = build_notification(result, findings_by_severity)
        try:
            await _feishu_notifier.send_summary(notification)
        except Exception as exc:
            logger.warning("feishu notification failed: %s", exc)

        return JSONResponse(
            {
                "trace_id": trace_id,
                "task_key": task_key,
                "repo": pr.repo,
                "pr_number": pr.pr_number,
                "changed_files": len(pr.changed_files),
                "findings_by_severity": findings_by_severity,
                "need_human_review": result.overall_need_human_review,
                "status": "accepted",
                "recommendation": getattr(result.report, "recommendation", None),
                "summary": getattr(result.report, "summary", "") or "",
            }
        )


def _record_from_result(result: Any) -> Any:
    from apps.code_review_pipeline.storage.review_store import ReviewRecord
    total_findings = 0
    for attr in ("triage", "static_analysis", "semantic_review", "test_coverage", "report"):
        section = getattr(result, attr, None)
        if section:
            total_findings += len(getattr(section, "findings", ()) or ())
    return ReviewRecord(
        trace_id=result.trace_id,
        repo=result.pr.repo,
        pr_number=result.pr.pr_number,
        head_sha=result.pr.head_sha,
        author=result.pr.author,
        findings_count=total_findings,
        need_human_review=result.overall_need_human_review,
        raw={},
    )
