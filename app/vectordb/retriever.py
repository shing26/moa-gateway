from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.vectordb import VectorDBClient, VectorDocument

logger = logging.getLogger("moa.vectordb.retriever")


@dataclass
class RetrievalResult:
    chunks: list[str]
    context: str  # concatenated chunks for injection into prompts
    doc_count: int


class ContextRetriever:
    """Retrieves relevant context chunks for a given query/session.

    Designed to be called before agent execution to enrich
    AgentEnvelope.global_summary with historical context.
    """

    def __init__(self, client: VectorDBClient | None = None, top_k: int = 5) -> None:
        self._client = client or VectorDBClient()
        self._top_k = top_k

    async def retrieve(
        self,
        query: str,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> RetrievalResult:
        filter_meta: dict[str, Any] = {}
        if session_id:
            filter_meta["session_id"] = session_id
        if user_id:
            filter_meta["user_id"] = user_id

        result = await self._client.search(query, top_k=self._top_k, filter_metadata=filter_meta or None)
        chunks = [doc.content for doc in result.documents]
        context = "\n\n---\n\n".join(chunks)
        logger.debug("retrieved %d chunks for query=%s session=%s", len(chunks), query[:50], session_id)
        return RetrievalResult(chunks=chunks, context=context, doc_count=len(chunks))

    async def retrieve_knowledge(self, query: str, top_k: int = 5) -> RetrievalResult:
        result = await self._client.search(
            query,
            top_k=top_k,
            filter_metadata={"source": "knowledge"},
        )
        chunks = [doc.content for doc in result.documents]
        context = "\n\n---\n\n".join(chunks)
        return RetrievalResult(chunks=chunks, context=context, doc_count=len(chunks))

    async def store_session_context(
        self,
        session_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        doc = VectorDocument(
            id=f"session:{session_id}:{hash(content)}",
            content=content,
            metadata={
                "session_id": session_id,
                **(metadata or {}),
            },
        )
        await self._client.upsert(doc)
        logger.debug("stored session context session=%s", session_id)


__all__ = ["ContextRetriever", "RetrievalResult"]
