from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class VectorDocument:
    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


@dataclass
class VectorSearchResult:
    documents: list[VectorDocument]


class VectorDBClient:
    """Abstract vector DB client. Uses in-memory dict for testing/dev."""

    def __init__(self) -> None:
        self._docs: dict[str, VectorDocument] = {}

    async def upsert(self, doc: VectorDocument) -> None:
        self._docs[doc.id] = doc

    async def upsert_batch(self, docs: list[VectorDocument]) -> None:
        for doc in docs:
            self._docs[doc.id] = doc

    async def search(self, query: str, top_k: int = 5, filter_metadata: dict[str, Any] | None = None) -> VectorSearchResult:
        if not self._docs:
            return VectorSearchResult(documents=[])
        # Simple keyword fallback when no real embedding is available.
        query_lower = query.lower()
        scored: list[VectorDocument] = []
        for doc in self._docs.values():
            if filter_metadata and not self._match_metadata(doc.metadata, filter_metadata):
                continue
            score = self._keyword_score(doc.content, query_lower)
            if score > 0:
                scored.append(VectorDocument(id=doc.id, content=doc.content, metadata=doc.metadata, score=score))
        scored.sort(key=lambda d: d.score, reverse=True)
        return VectorSearchResult(documents=scored[:top_k])

    async def delete_by_metadata(self, filter_metadata: dict[str, Any]) -> int:
        to_delete = [doc_id for doc_id, doc in self._docs.items() if self._match_metadata(doc.metadata, filter_metadata)]
        for doc_id in to_delete:
            del self._docs[doc_id]
        return len(to_delete)

    async def get(self, doc_id: str) -> VectorDocument | None:
        return self._docs.get(doc_id)

    async def clear(self) -> None:
        self._docs.clear()

    @property
    def count(self) -> int:
        return len(self._docs)

    @staticmethod
    def _keyword_score(content: str, query_lower: str) -> float:
        content_lower = content.lower()
        score = 0.0
        for word in query_lower.split():
            count = content_lower.count(word)
            score += count * 0.1
        return score

    @staticmethod
    def _match_metadata(doc_meta: dict[str, Any], filter_meta: dict[str, Any]) -> bool:
        return all(doc_meta.get(k) == v for k, v in filter_meta.items())


__all__ = ["VectorDBClient", "VectorDocument", "VectorSearchResult"]
