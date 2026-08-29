from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.vectordb.keywords import keyword_score, match_metadata

logger = logging.getLogger("moa.vectordb")


@dataclass
class VectorDocument:
    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


@dataclass
class VectorSearchResult:
    documents: list[VectorDocument]


class VectorStore(Protocol):
    """The contract every backend implements.

    Kept here, next to ``VectorDBClient``, so the shape is obvious. Consumers
    (``ContextRetriever``, ``KnowledgeBase``) stay duck-typed against it.
    """

    backend: str

    async def upsert(self, doc: VectorDocument) -> None: ...
    async def upsert_batch(self, docs: list[VectorDocument]) -> None: ...
    async def search(
        self, query: str, top_k: int = 5, filter_metadata: dict[str, Any] | None = None
    ) -> VectorSearchResult: ...
    async def find_by_metadata(
        self, filter_metadata: dict[str, Any], limit: int = 100
    ) -> list[VectorDocument]: ...
    async def delete_by_metadata(self, filter_metadata: dict[str, Any]) -> int: ...
    async def get(self, doc_id: str) -> VectorDocument | None: ...
    async def clear(self) -> None: ...
    async def start(self) -> None: ...
    async def close(self) -> None: ...


class VectorDBClient:
    """In-memory vector store: the zero-configuration default.

    Behaviourally this is the baseline the PostgreSQL backend must match --
    including the ``searchable`` exclusion and the metadata AND-equality
    filter, both of which now mirror the SQL implementation.
    """

    backend = "memory"

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
        query_lower = query.lower()
        scored: list[VectorDocument] = []
        for doc in self._docs.values():
            if doc.metadata.get("searchable") is False:
                continue
            if filter_metadata and not match_metadata(doc.metadata, filter_metadata):
                continue
            score = keyword_score(doc.content, query_lower)
            if score > 0:
                scored.append(VectorDocument(id=doc.id, content=doc.content, metadata=doc.metadata, score=score))
        scored.sort(key=lambda d: d.score, reverse=True)
        return VectorSearchResult(documents=scored[:top_k])

    async def find_by_metadata(
        self, filter_metadata: dict[str, Any], limit: int = 100
    ) -> list[VectorDocument]:
        """List rows matching a metadata filter, without any query scoring."""
        found = [
            doc
            for doc in self._docs.values()
            if match_metadata(doc.metadata, filter_metadata)
        ]
        return found[:limit]

    async def delete_by_metadata(self, filter_metadata: dict[str, Any]) -> int:
        to_delete = [doc_id for doc_id, doc in self._docs.items() if match_metadata(doc.metadata, filter_metadata)]
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

    async def start(self) -> None:
        """No-op: the in-memory store has no external resource to open."""

    async def close(self) -> None:
        """No-op, matching ``start``."""

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "degraded": False,
            "reason": None,
            "embedding": False,
        }


def build_vector_client(settings_obj: Any | None = None) -> VectorStore:
    """Choose a backend from configuration.

    Empty ``VECTOR_DB_DSN`` selects the in-memory store, which is the current
    behaviour and keeps every existing test and local run unchanged. Setting a
    DSN switches to PostgreSQL without touching a single consumer -- that is
    what makes rolling this out a config change rather than a code change.
    """
    if settings_obj is None:
        from app.config import settings as settings_obj  # type: ignore[no-redef]

    dsn = getattr(settings_obj, "vector_db_dsn", "") or ""
    if not dsn:
        logger.info("vectordb: VECTOR_DB_DSN 未配置，使用内存存储（进程重启即丢失）")
        return VectorDBClient()

    try:
        from app.vectordb.embeddings import build_embedding_provider
        from app.vectordb.pgvector_client import PgVectorClient
    except ImportError as exc:
        logger.warning("vectordb: 后端依赖不可用，回退内存存储: %s", exc)
        return VectorDBClient()

    return PgVectorClient(
        dsn=dsn,
        table=getattr(settings_obj, "vector_db_table", "gateway_documents"),
        dim=int(getattr(settings_obj, "vector_db_embedding_dim", 1536)),
        embedding=build_embedding_provider(settings_obj),
        pool_min_size=int(getattr(settings_obj, "vector_db_pool_min_size", 1)),
        pool_max_size=int(getattr(settings_obj, "vector_db_pool_max_size", 4)),
        keyword_scan_limit=int(getattr(settings_obj, "vector_db_keyword_scan_limit", 2000)),
        strict=bool(getattr(settings_obj, "vector_db_strict", False)),
        auto_migrate=bool(getattr(settings_obj, "vector_db_auto_migrate", True)),
    )


__all__ = [
    "VectorDBClient",
    "VectorDocument",
    "VectorSearchResult",
    "VectorStore",
    "build_vector_client",
]
