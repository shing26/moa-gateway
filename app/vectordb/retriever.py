from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.vectordb import VectorDBClient, VectorDocument
from app.vectordb.rerank import NoopReranker, Reranker

logger = logging.getLogger("moa.vectordb.retriever")


def _effective_reranker(reranker: Reranker | None) -> Reranker | None:
    """把 ``NoopReranker`` 折叠成 ``None``。

    NoopReranker 的语义就是"不精排"，必须等价于"没有装精排器"。否则关闭态也会
    取宽候选：pgvector 每条腿的 LIMIT 会在 ``CANDIDATE_MULTIPLIER`` 上再叠一层
    （top_k=5 时从 20 涨到 80），而返回内容一分不变——排序是全序的
    （融合分降序 + doc.id 次级键），取 20 截 5 与直接取 5 结果相同。
    白付的检索代价不该由"默认关闭"来承担。
    """
    if isinstance(reranker, NoopReranker):
        return None
    return reranker


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

    def __init__(
        self,
        client: VectorDBClient | None = None,
        top_k: int = 5,
        reranker: Reranker | None = None,
    ) -> None:
        self._client = client or VectorDBClient()
        self._top_k = top_k
        self._reranker = _effective_reranker(reranker)

    @property
    def reranker(self) -> Reranker | None:
        return self._reranker

    @reranker.setter
    def reranker(self, value: Reranker | None) -> None:
        self._reranker = _effective_reranker(value)

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

        # 有精排器才取宽候选：精排需要挑选空间。无精排器时按 top_k 取，
        # 与装精排器之前完全一致。
        candidate_k = max(self._top_k * 4, 20) if self._reranker else self._top_k
        result = await self._client.search(
            query, top_k=candidate_k, filter_metadata=filter_meta or None
        )
        docs = list(result.documents)
        if self._reranker is not None:
            docs = list(await self._reranker.rerank(query, docs, self._top_k))
        docs = docs[: self._top_k]
        chunks = [doc.content for doc in docs]
        context = "\n\n---\n\n".join(chunks)
        logger.debug("retrieved %d chunks for query=%s session=%s", len(chunks), query[:50], session_id)
        return RetrievalResult(chunks=chunks, context=context, doc_count=len(chunks))

    async def retrieve_knowledge(self, query: str, top_k: int = 5) -> RetrievalResult:
        candidate_k = max(top_k * 4, 20) if self._reranker else top_k
        result = await self._client.search(
            query,
            top_k=candidate_k,
            filter_metadata={"source": "knowledge"},
        )
        docs = list(result.documents)
        if self._reranker is not None:
            docs = list(await self._reranker.rerank(query, docs, top_k))
        docs = docs[:top_k]
        chunks = [doc.content for doc in docs]
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
