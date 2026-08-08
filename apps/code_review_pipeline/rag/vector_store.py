from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from apps.code_review_pipeline.rag.embeddings import EmbeddingResult, EmbeddingError

logger = logging.getLogger("moa.code_review.rag")


class VectorStoreInitError(Exception):
    """Raised when the vector store cannot be initialized."""


class VectorStore:
    def upsert(self, items: list[EmbeddingResult], *, trace_id: str, source_type: str) -> None:
        raise NotImplementedError

    def search(self, vector: tuple[float, ...], *, limit: int = 5) -> list[dict[str, Any]]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class PgVectorStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn = None

    def _connect(self) -> None:
        if self._conn is None:
            import psycopg  # type: ignore[import-untyped]
            self._conn = psycopg.connect(self._dsn)

    def upsert(self, items: list[EmbeddingResult], *, trace_id: str, source_type: str) -> None:
        self._connect()
        try:
            with self._conn.cursor() as cur:
                for item in items:
                    cur.execute(
                        """
                        INSERT INTO code_review_vectors (trace_id, source_type, source_id, content, embedding, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            trace_id,
                            source_type,
                            item.source_id,
                            item.text,
                            list(item.vector),
                            json.dumps(item.metadata or {}, ensure_ascii=False),
                        ),
                    )
                self._conn.commit()
        except Exception as exc:
            if self._conn:
                self._conn.rollback()
            raise VectorStoreInitError(f"pgvector upsert failed: {exc}") from exc

    def search(self, vector: tuple[float, ...], *, limit: int = 5) -> list[dict[str, Any]]:
        self._connect()
        try:
            import psycopg  # type: ignore[import-untyped]
            from psycopg.rows import dict_row  # type: ignore[import-untyped]
            with self._conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT trace_id, source_type, source_id, content, metadata, created_at,
                           1 - (embedding <=> %s::vector) AS score
                    FROM code_review_vectors
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (list(vector), list(vector), limit),
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception as exc:
            logger.error("pgvector search failed: %s", exc)
            return []

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []

    def upsert(self, items: list[EmbeddingResult], *, trace_id: str, source_type: str) -> None:
        for item in items:
            self._items.append(
                {
                    "trace_id": trace_id,
                    "source_type": source_type,
                    "source_id": item.source_id,
                    "content": item.text,
                    "vector": item.vector,
                    "metadata": item.metadata or {},
                    "score": 0.0,
                }
            )

    def search(self, vector: tuple[float, ...], *, limit: int = 5) -> list[dict[str, Any]]:
        scored = []
        for item in self._items:
            score = self._cosine(vector, item["vector"])
            scored.append({**item, "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    @staticmethod
    def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(y * y for y in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


def build_vector_store() -> VectorStore:
    dsn = (
        os.getenv("CODE_REVIEW_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or ""
    )
    if not dsn:
        logger.info("no database URL configured; using in-memory vector store")
        return InMemoryVectorStore()
    try:
        import psycopg  # noqa: F401
    except Exception as exc:
        raise VectorStoreInitError(f"psycopg is required for pgvector store: {exc}") from exc
    return PgVectorStore(dsn=dsn)
