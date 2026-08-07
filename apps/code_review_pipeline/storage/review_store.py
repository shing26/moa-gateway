from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("moa.code_review.storage")


@dataclass(frozen=True)
class ReviewRecord:
    trace_id: str
    repo: str
    pr_number: int
    head_sha: str
    author: str
    findings_count: int
    need_human_review: bool
    raw: dict[str, Any]


class ReviewStore:
    def __init__(self) -> None:
        self._records: dict[str, ReviewRecord] = {}

    def save(self, record: ReviewRecord) -> None:
        self._records[record.trace_id] = record
        logger.info("review saved trace=%s findings=%d", record.trace_id, record.findings_count)

    def get(self, trace_id: str) -> ReviewRecord | None:
        return self._records.get(trace_id)

    async def close(self) -> None:
        self._records.clear()
