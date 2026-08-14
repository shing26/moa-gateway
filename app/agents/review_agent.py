from __future__ import annotations

import logging
import os
import re

from app.agents.contract import AgentEnvelope
from app.models.events import MoAEvent
from apps.code_review_pipeline.agents.code_review_pipeline import CodeReviewPipeline
from apps.code_review_pipeline.reporting import format_review_summary

logger = logging.getLogger("moa.agents.review")

_PR_PATTERN = re.compile(r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<pr>\d+)")


class ReviewAgent:
    def _build_pipeline(self) -> CodeReviewPipeline:
        return CodeReviewPipeline.from_env()

    async def execute(self, envelope: AgentEnvelope) -> str:
        if not os.getenv("GITHUB_TOKEN"):
            return "PR 审查需要配置 GITHUB_TOKEN；请设置后重试。"
        raw = envelope.user_raw_input.strip()
        body = envelope.agent_local_slot.get("webhook_body")
        if not isinstance(body, dict):
            body = {}
        match = _PR_PATTERN.search(raw)
        if match and not body:
            pr_number = int(match.group("pr"))
            body = {
                "action": "opened",
                "number": pr_number,
                "pull_request": {"number": pr_number, "title": raw},
                "repository": {"full_name": match.group("repo")},
            }
        if not body:
            return "PR 审查请提供 owner/repo#PR号，例如 shing26/moa-gateway#1。"
        event = MoAEvent(
            trace_id=envelope.trace_id or "review_agent",
            event=None,
            session_id=envelope.session_id or "review",
            text=raw,
            context=body,
        )
        try:
            pipeline = self._build_pipeline()
            _, result = await pipeline.run(event)
            return format_review_summary(result)
        except Exception as exc:
            logger.warning("review agent failed: %s", exc)
            return f"PR 审查执行失败：{exc}"
