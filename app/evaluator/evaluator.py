from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EvalResult:
    score: float
    need_human_review: bool
    issues: tuple[str, ...] = ()


class Evaluator(Protocol):
    async def score(self, output_text: str, intent: str) -> EvalResult: ...


class RuleEvaluator:
    MAX_RETRY = 2

    async def score(self, output_text: str, intent: str) -> EvalResult:
        issues: list[str] = []
        if not output_text or not output_text.strip():
            issues.append("empty_output")
        if len(output_text) > 20000:
            issues.append("output_too_long")

        try:
            if self._looks_json(output_text):
                json.loads(output_text)
        except json.JSONDecodeError:
            issues.append("invalid_json_payload")

        if "TODO" in output_text or "FIXME" in output_text:
            issues.append("contains_unfinished_marker")

        need_human_review = bool(issues)
        score = 1.0 if not issues else 0.3
        return EvalResult(score=score, need_human_review=need_human_review, issues=tuple(issues))

    @staticmethod
    def _looks_json(text: str) -> bool:
        text = text.strip()
        return text.startswith("{") or text.startswith("[")
