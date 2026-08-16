from __future__ import annotations

import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.deps import logger, tracer
from app.models.events import MoAEvent, PlatformEvent
from apps.code_review_pipeline.reporting import build_notification, count_by_severity
from apps.code_review_pipeline.agents.code_review_pipeline import CodeReviewPipeline
from apps.code_review_pipeline.notifications.feishu_notifier import FeishuReviewNotifier
from apps.code_review_pipeline.storage.review_store import ReviewStore, build_review_store

github_review_router = APIRouter()
logger = logging.getLogger("moa.code_review.route")

_review_store = build_review_store()
_feishu_notifier = FeishuReviewNotifier.from_env()


@github_review_router.post("/webhook/github/review")
async def github_review_webhook(request: Request) -> JSONResponse:
    with tracer.start_as_current_span("moa.code_review.webhook") as span:
        body = await request.json()
        platform_event = PlatformEvent(
            platform="github",
            message_id=str(body.get("id", "")),
            session_id=str(body.get("repository", {}).get("full_name", "")),
            user_id=str(body.get("pull_request", {}).get("user", {}).get("login", "")),
            payload=body,
        )
        trace_id = f"cr_{platform_event.session_id}:{platform_event.message_id}"
        span.set_attribute("moa.channel", "github")
        span.set_attribute("moa.trace_id", trace_id)

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

        _review_store.save(_record_from_result(result))

        findings_by_severity = count_by_severity(result)
        notification = build_notification(result, findings_by_severity)
        try:
            await _feishu_notifier.send_summary(notification)
        except Exception as exc:
            logger.warning("feishu notification failed: %s", exc)

        return JSONResponse(
            {
                "trace_id": trace_id,
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
