from __future__ import annotations

import json
import logging
import os
import sqlite3
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
                        ON CONFLICT (trace_id, source_id) DO UPDATE SET
                            source_type = EXCLUDED.source_type,
                            content = EXCLUDED.content,
                            embedding = EXCLUDED.embedding,
                            metadata = EXCLUDED.metadata,
                            created_at = NOW()
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
    def __init__(self, db_path: str | None = None) -> None:
        self._items: list[dict[str, Any]] = []
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        if db_path:
            self._conn = self._init_sqlite(db_path)
            self._load_from_sqlite()

    @staticmethod
    def _init_sqlite(db_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS code_review_vectors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT,
                source_type TEXT,
                source_id TEXT,
                content TEXT,
                embedding TEXT,
                metadata TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_code_review_vectors_source
            ON code_review_vectors (trace_id, source_id)
            """
        )
        conn.commit()
        return conn

    def _load_from_sqlite(self) -> None:
        if not self._conn:
            return
        try:
            rows = self._conn.execute(
                "SELECT trace_id, source_type, source_id, content, embedding, metadata FROM code_review_vectors"
            ).fetchall()
            for row in rows:
                trace_id, source_type, source_id, content, embedding, metadata = row
                self._items.append(
                    {
                        "trace_id": trace_id,
                        "source_type": source_type,
                        "source_id": source_id,
                        "content": content,
                        "vector": tuple(json.loads(embedding)) if embedding else (),
                        "metadata": json.loads(metadata) if metadata else {},
                        "score": 0.0,
                    }
                )
        except Exception as exc:
            logger.error("failed to load sqlite vector store: %s", exc)

    def _upsert_to_sqlite(self, item: dict[str, Any]) -> None:
        if not self._conn:
            return
        try:
            self._conn.execute(
                """
                INSERT INTO code_review_vectors (trace_id, source_type, source_id, content, embedding, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id, source_id) DO UPDATE SET
                    content = excluded.content,
                    embedding = excluded.embedding,
                    metadata = excluded.metadata
                """,
                (
                    item.get("trace_id"),
                    item.get("source_type"),
                    item.get("source_id"),
                    item.get("content"),
                    json.dumps(item.get("vector", ()), ensure_ascii=False),
                    json.dumps(item.get("metadata", {}), ensure_ascii=False),
                ),
            )
            self._conn.commit()
        except Exception as exc:
            logger.error("sqlite upsert failed: %s", exc)

    def upsert(self, items: list[EmbeddingResult], *, trace_id: str, source_type: str) -> None:
        for item in items:
            record = {
                "trace_id": trace_id,
                "source_type": source_type,
                "source_id": item.source_id,
                "content": item.text,
                "vector": item.vector,
                "metadata": item.metadata or {},
                "score": 0.0,
            }
            self._items = [
                existing
                for existing in self._items
                if not (
                    existing.get("trace_id") == trace_id
                    and existing.get("source_id") == item.source_id
                )
            ]
            self._items.append(record)
            self._upsert_to_sqlite(record)

    def search(self, vector: tuple[float, ...], *, limit: int = 5) -> list[dict[str, Any]]:
        scored = []
        for item in self._items:
            score = self._cosine(vector, item["vector"])
            scored.append({**item, "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

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
        db_path = os.getenv("CODE_REVIEW_VECTOR_DB_PATH")
        if db_path:
            return InMemoryVectorStore(db_path=db_path)
        return InMemoryVectorStore()

    try:
        import psycopg  # noqa: F401
    except Exception as exc:
        raise VectorStoreInitError(f"psycopg is required for pgvector store: {exc}") from exc
    return PgVectorStore(dsn=dsn)
