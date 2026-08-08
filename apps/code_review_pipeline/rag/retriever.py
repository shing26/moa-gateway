from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.agents.contract import AgentEnvelope
from apps.code_review_pipeline.rag.embeddings import generate_embeddings, EmbeddingError
from apps.code_review_pipeline.rag.vector_store import build_vector_store

logger = logging.getLogger("moa.code_review.rag")


class RetrievalResult:
    def __init__(self, context: str, chunks: list[dict[str, Any]], doc_count: int) -> None:
        self.context = context
        self.chunks = chunks
        self.doc_count = doc_count


async def retrieve_team_patterns(envelope: AgentEnvelope, *, limit: int = 5) -> RetrievalResult:
    """
    Retrieve team-specific review patterns for the current PR context.
    """
    query = envelope.user_raw_input or envelope.agent_local_slot.get("pr_title", "")
    diff = envelope.agent_local_slot.get("diff", "")
    query_text = "\n".join(part for part in [query, diff[:2000]] if part).strip()
    if not query_text:
        return RetrievalResult(context="", chunks=[], doc_count=0)

    store = build_vector_store()
    try:
        try:
            embeddings = await generate_embeddings([query_text])
        except EmbeddingError as exc:
            logger.warning("embedding generation failed, RAG disabled: %s", exc)
            return RetrievalResult(context="", chunks=[], doc_count=0)

        if not embeddings:
            return RetrievalResult(context="", chunks=[], doc_count=0)

        results = store.search(embeddings[0].vector, limit=limit)
    finally:
        store.close()

    if not results:
        return RetrievalResult(context="", chunks=[], doc_count=0)

    lines = ["团队历史相似案例/规范："]
    for idx, item in enumerate(results, start=1):
        content = item.get("content", "")
        score = item.get("score", 0.0)
        source_id = item.get("source_id", "")
        lines.append(f"{idx}. [score={score:.2f}] {content}")
        lines.append(f"   source_id={source_id}")

    context = "\n".join(lines)
    return RetrievalResult(context=context, chunks=results, doc_count=len(results))
