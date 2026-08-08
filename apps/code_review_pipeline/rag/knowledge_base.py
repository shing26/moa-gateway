from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from apps.code_review_pipeline.rag.embeddings import generate_embeddings, EmbeddingResult, EmbeddingError
from apps.code_review_pipeline.rag.vector_store import build_vector_store

logger = logging.getLogger("moa.code_review.rag")


@dataclass(frozen=True)
class KnowledgeDoc:
    source_type: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any] | None = None


class KnowledgeBase:
    def __init__(self, store: Any) -> None:
        self._store = store

    async def ingest_documents(self, docs: list[KnowledgeDoc]) -> int:
        texts = [doc.content for doc in docs if doc.content]
        if not texts:
            return 0

        try:
            embeddings = await generate_embeddings(texts)
        except EmbeddingError as exc:
            logger.error("failed to generate embeddings for knowledge base: %s", exc)
            return 0

        items: list[EmbeddingResult] = []
        for idx, (doc, emb) in enumerate(zip(docs, embeddings)):
            items.append(
                EmbeddingResult(
                    text=doc.content,
                    vector=emb.vector,
                    source_type=doc.source_type,
                    source_id=doc.source_id,
                    metadata={
                        "title": doc.title,
                        "source_type": doc.source_type,
                        "source_id": doc.source_id,
                        **(doc.metadata or {}),
                    },
                )
            )

        try:
            self._store.upsert(items, trace_id="knowledge-base", source_type="knowledge")
            return len(items)
        except Exception as exc:
            logger.error("failed to upsert embeddings: %s", exc)
            return 0

    async def list_docs(self) -> list[dict[str, Any]]:
        try:
            results = self._store.search(tuple([0.0] * 1536), limit=1000)
            return [
                {
                    "source_id": item.get("source_id", ""),
                    "content": item.get("content", "")[:200],
                    "metadata": item.get("metadata", {}),
                }
                for item in results
            ]
        except Exception as exc:
            logger.error("failed to list docs: %s", exc)
            return []

    async def delete_doc(self, source_id: str) -> bool:
        # pgvector delete path can be added later; no-op for in-memory store.
        return False


def build_knowledge_base() -> KnowledgeBase:
    return KnowledgeBase(store=build_vector_store())
