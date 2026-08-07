from __future__ import annotations

import logging
from typing import Any

from app.agents.provider import LLMClient, LLMConfig
from apps.code_review_pipeline.routing.fallback_llm import FallbackLLMClient

logger = logging.getLogger("moa.code_review.llm")


def build_code_review_llm() -> LLMClient | FallbackLLMClient:
    primary = LLMClient(LLMConfig.from_env("CODE_REVIEW"))
    fallback_key = "CODE_REVIEW_FALLBACK_API_KEY"
    import os
    if os.getenv(fallback_key) or os.getenv("CODE_REVIEW_FALLBACK_MODEL"):
        fallback = LLMClient(LLMConfig.from_env("CODE_REVIEW_FALLBACK"))
        return FallbackLLMClient(primary=primary, fallback=fallback)
    return primary
