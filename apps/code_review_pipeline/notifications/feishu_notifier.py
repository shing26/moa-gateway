from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("moa.code_review.notifications")


@dataclass(frozen=True)
class ReviewNotification:
    trace_id: str
    repo: str
    pr_number: int
    author: str
    changed_files: int
    overall_need_human_review: bool = False
    findings_by_severity: dict[str, int] | None = None


class FeishuReviewNotifier:
    def __init__(self, webhook_url: str | None = None) -> None:
        self._webhook_url = webhook_url

    async def send_summary(self, notification: ReviewNotification) -> None:
        logger.info(
            "feishu summary trace=%s repo=%s pr=%d author=%s files=%d need_review=%s",
            notification.trace_id,
            notification.repo,
            notification.pr_number,
            notification.author,
            notification.changed_files,
            notification.overall_need_human_review,
        )

        if self._webhook_url is None:
            return

        # TODO Week2: call Feishu card API with risk-tiered card payload.
        # Low/Medium -> summary card; High/Critical -> HITL approval card.
        raise NotImplementedError("Feishu card delivery is not implemented yet")

    @staticmethod
    def from_env() -> FeishuReviewNotifier:
        import os

        return FeishuReviewNotifier(webhook_url=os.getenv("FEISHU_REVIEW_WEBHOOK"))
