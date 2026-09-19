"""PostgreSQL + pgvector backend for the gateway retrieval store.

Interface parity
----------------
This class is a drop-in replacement for the in-memory ``VectorDBClient``:
same coroutine signatures, same ``VectorDocument`` / ``VectorSearchResult``
types, so ``ContextRetriever`` and ``KnowledgeBase`` never learn which backend
they are talking to. Two deliberate exceptions:

* ``count`` is a coroutine here (``acount()``) because it needs a round trip.
  The in-memory client still exposes a synchronous property for its tests.
* ``search`` gains real hybrid ranking (see below).

Hybrid ranking (root cause C)
-----------------------------
Vector similarity alone is weak on exact identifiers — error codes, config
keys, proper nouns — where lexical matching is strong, and vice versa. So
``search`` fetches vector candidates, then re-scores them with the shared
BD-01 keyword scorer and fuses the two signals:

    final = 0.7 * cosine + 0.3 * normalised_keyword

When no embedding provider is configured (or it is circuit-broken, or it
returns the wrong dimensionality) the vector leg is skipped entirely and we
degrade to a bounded keyword scan. That path is *correct but degraded*, not a
stub: it is the same scorer the in-memory store has always used.

The keyword scan has a row cap (``keyword_scan_limit``) because it is O(N).
Hitting the cap logs a warning rather than silently truncating: a silent wrong
answer is worse than a slow one.

Excluding non-corpus rows
-------------------------
Rows carrying ``metadata.searchable == false`` are directory entries (the
knowledge-base manifest), not retrievable corpus. Both backends apply this
filter identically so a manifest can never be returned as a context chunk.

On the B608 suppression markers
-------------------------------
The table name is configurable, so it cannot be a bind parameter and has to be
interpolated. Bandit flags every such f-string as possible SQL injection
because it cannot see that ``_validate_identifier()`` already rejected anything
outside ``[A-Za-z_][A-Za-z0-9_]*`` in ``__init__``. Each interpolation site
carries a narrow suppression marker rather than a project-wide skip, so new
SQL stays flagged and these eight sites remain individually auditable.
``tests/unit/test_pgvector_client.py`` asserts the validator rejects injection
attempts -- that test is the real guarantee, not the marker.

(The marker text is deliberately not spelled literally here: the scanner
matches it by raw-text regex, even inside docstrings, which would otherwise
register phantom suppressions.)
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
from typing import Any, Sequence

from app.vectordb import VectorDocument, VectorSearchResult
from app.vectordb.keywords import keyword_score, normalized_keyword_score

logger = logging.getLogger("moa.vectordb.postgres")

# 用 fullmatch 配合无锚点模式，而不是 `^...$` + match：Python 正则里 `$`
# 还会匹配结尾换行符之前的位置，所以 `^...$` 会放过 "gateway_documents\n"。
# 这里是 8 处 B608 抑制标记所依赖的校验，必须严格。
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[2] / "db" / "gateway_schema.sql"

# Fusion weights. Vector carries the semantic load; keyword rescues exact
# identifiers that embeddings tend to smear together.
VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3

# How many vector candidates to pull before re-ranking. Wider than top_k so the
# keyword term has something to reorder; capped so a huge table stays cheap.
CANDIDATE_MULTIPLIER = 4
MIN_CANDIDATES = 20

_SEARCHABLE_SQL = "(metadata->>'searchable') IS DISTINCT FROM 'false'"


class VectorStoreUnavailable(RuntimeError):
    """Raised when a store operation is attempted against a degraded backend."""


def _validate_identifier(name: str) -> str:
    """Guard against SQL injection through the configurable table name."""
    if not _IDENT_RE.fullmatch(name or ""):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def _vector_literal(vector: Sequence[float]) -> str:
    """Render a vector in pgvector's text input format: ``[1,2,3]``.

    Sent as text and cast with ``::vector`` on the server. Passing a Python
    list directly would be adapted to a Postgres ARRAY, which has no cast to
    vector.
    """
    return "[" + ",".join(f"{value:.8g}" for value in vector) + "]"


def fuse(vector_score: float, keyword_score_01: float) -> float:
    return VECTOR_WEIGHT * max(vector_score, 0.0) + KEYWORD_WEIGHT * keyword_score_01


def split_statements(raw_sql: str) -> list[str]:
    """Split a DDL script on semicolons, dropping whole-line comments."""
    lines = [line for line in raw_sql.splitlines() if not line.lstrip().startswith("--")]
    return [chunk.strip() for chunk in "\n".join(lines).split(";") if chunk.strip()]


def render_schema(raw_sql: str, dim: int) -> str:
    """Bind the configured embedding dimension into the pgvector DDL."""
    if int(dim) <= 0:
        raise ValueError(f"embedding dimension must be positive: {dim!r}")
    return raw_sql.replace("vector(1536)", f"vector({int(dim)})")


class PgVectorClient:
    """Async PostgreSQL vector store backed by a psycopg connection pool."""

    backend = "postgres"

    def __init__(
        self,
        dsn: str,
        *,
        table: str = "gateway_documents",
        dim: int = 1536,
        embedding: Any | None = None,
        pool_min_size: int = 1,
        pool_max_size: int = 4,
        open_timeout_s: float = 10.0,
        keyword_scan_limit: int = 2000,
        strict: bool = False,
        auto_migrate: bool = True,
        pool: Any | None = None,
    ) -> None:
        self._dsn = dsn
        self._table = _validate_identifier(table)
        self._dim = int(dim)
        self._embedding = embedding
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._open_timeout_s = open_timeout_s
        self._keyword_scan_limit = int(keyword_scan_limit)
        self._strict = strict
        self._auto_migrate = auto_migrate
        self._pool = pool
        self._pool_injected = pool is not None
        self._degraded_reason: str | None = None

    # ── introspection ──────────────────────────────────────────────────────

    @property
    def is_degraded(self) -> bool:
        return self._pool is None

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    def describe(self) -> dict[str, Any]:
        provider = self._embedding
        return {
            "backend": self.backend,
            "table": self._table,
            "dim": self._dim,
            "degraded": self.is_degraded,
            "reason": self._degraded_reason,
            "embedding": bool(provider is not None and getattr(provider, "enabled", False)),
            "embedding_model": getattr(provider, "model", None),
        }

    @property
    def count(self) -> int:
        raise NotImplementedError(
            "PgVectorClient 的计数需要访问数据库，请使用 `await client.acount()`"
        )

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open the pool and (optionally) apply the schema.

        Never raises unless ``strict`` is set. A non-strict failure marks the
        client degraded, which is surfaced on /healthz, so the gateway keeps
        serving instead of failing to boot because a database is unreachable.
        """
        if self._pool is not None or self._pool_injected:
            return
        if not self._dsn:
            self._degraded_reason = "VECTOR_DB_DSN is empty"
            return

        try:
            from psycopg_pool import AsyncConnectionPool
        except ImportError as exc:
            self._degraded_reason = f"psycopg not installed: {exc}"
            logger.critical("vectordb: %s", self._degraded_reason)
            if self._strict:
                raise
            return

        pool = AsyncConnectionPool(
            self._dsn,
            min_size=self._pool_min_size,
            max_size=self._pool_max_size,
            open=False,
            timeout=self._open_timeout_s,
        )
        try:
            await pool.open(wait=True, timeout=self._open_timeout_s)
            self._pool = pool
            if self._auto_migrate:
                await self._ensure_schema()
        except Exception as exc:  # noqa: BLE001 - startup must not explode
            self._pool = None
            try:
                await pool.close()
            except Exception:  # noqa: BLE001, S110 - pool is already broken
                pass
            self._degraded_reason = f"{type(exc).__name__}: {exc}"[:300]
            logger.critical(
                "vectordb: PostgreSQL 不可用，本进程退化为无持久化检索：%s", self._degraded_reason
            )
            if self._strict:
                raise
            return

        self._degraded_reason = None
        logger.info(
            "vectordb: PostgreSQL 连接池就绪 (table=%s, dim=%d, embedding=%s)",
            self._table,
            self._dim,
            "on" if (self._embedding and self._embedding.enabled) else "off",
        )

    async def _ensure_schema(self) -> None:
        if not _SCHEMA_PATH.exists():
            logger.warning("vectordb: 未找到 schema 文件 %s，跳过自动建表", _SCHEMA_PATH)
            return
        schema_sql = render_schema(_SCHEMA_PATH.read_text(encoding="utf-8"), self._dim)
        for statement in split_statements(schema_sql):
            try:
                await self._run(statement, None, fetch=False)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "vectordb: DDL 执行失败，请由 DBA 手工执行 %s 后重启：%s", _SCHEMA_PATH, exc
                )
                raise

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None and not self._pool_injected:
            await pool.close()
        # 归还 embedding 的 HTTP 连接，否则进程退出时会留下未关闭的 socket。
        provider = self._embedding
        if provider is not None and hasattr(provider, "aclose"):
            await provider.aclose()

    # ── transport ──────────────────────────────────────────────────────────

    async def _run(self, sql: str, params: Sequence[Any] | None, *, fetch: bool) -> Any:
        """Execute one statement. The single seam tests patch to avoid a live DB."""
        if self._pool is None:
            raise VectorStoreUnavailable(self._degraded_reason or "postgres pool is not open")
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            if fetch:
                return await cur.fetchall()
            return cur.rowcount

    async def _run_many(self, sql: str, rows: list[tuple]) -> None:
        if self._pool is None:
            raise VectorStoreUnavailable(self._degraded_reason or "postgres pool is not open")
        if not rows:
            return
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(sql, rows)

    # ── writes ─────────────────────────────────────────────────────────────

    async def upsert(self, doc: VectorDocument) -> None:
        await self.upsert_batch([doc])

    async def upsert_batch(self, docs: list[VectorDocument]) -> None:
        if not docs:
            return
        vectors = await self._embed_batch([doc.content for doc in docs])
        rows = [
            (
                doc.id,
                doc.content,
                json.dumps(doc.metadata, ensure_ascii=False),
                _vector_literal(vec) if vec is not None else None,
            )
            for doc, vec in zip(docs, vectors)
        ]
        # COALESCE on the conflict path matters: if the embedding call failed
        # transiently we must not overwrite a previously stored good vector
        # with NULL, or an outage would silently erase the semantic index.
        await self._run_many(
            f"INSERT INTO {self._table} (id, content, metadata, embedding) "  # nosec B608 - 表名已校验
            f"VALUES (%s, %s, %s::jsonb, %s::vector) "
            f"ON CONFLICT (id) DO UPDATE SET "
            f"content = EXCLUDED.content, "
            f"metadata = EXCLUDED.metadata, "
            f"embedding = COALESCE(EXCLUDED.embedding, {self._table}.embedding), "
            f"updated_at = now()",
            rows,
        )

    async def delete_by_metadata(self, filter_metadata: dict[str, Any]) -> int:
        deleted = await self._run(
            f"DELETE FROM {self._table} WHERE metadata @> %s::jsonb",  # nosec B608 - 表名已校验
            (json.dumps(filter_metadata, ensure_ascii=False),),
            fetch=False,
        )
        return int(deleted or 0)

    async def clear(self) -> None:
        await self._run(f"DELETE FROM {self._table}", None, fetch=False)  # nosec B608 - 表名已校验

    # ── reads ──────────────────────────────────────────────────────────────

    async def get(self, doc_id: str) -> VectorDocument | None:
        rows = await self._run(
            f"SELECT id, content, metadata FROM {self._table} WHERE id = %s",  # nosec B608 - 表名已校验
            (doc_id,),
            fetch=True,
        )
        if not rows:
            return None
        return VectorDocument(id=rows[0][0], content=rows[0][1], metadata=rows[0][2] or {})

    async def find_by_metadata(
        self, filter_metadata: dict[str, Any], limit: int = 100
    ) -> list[VectorDocument]:
        rows = await self._run(
            f"SELECT id, content, metadata FROM {self._table} "  # nosec B608 - 表名已校验
            f"WHERE metadata @> %s::jsonb LIMIT %s",
            (json.dumps(filter_metadata, ensure_ascii=False), int(limit)),
            fetch=True,
        )
        return [
            VectorDocument(id=row[0], content=row[1], metadata=row[2] or {}) for row in rows or []
        ]

    async def acount(self) -> int:
        rows = await self._run(
            f"SELECT count(*) FROM {self._table}", None, fetch=True  # nosec B608 - 表名已校验
        )
        return int(rows[0][0]) if rows else 0

    async def search(
        self,
        query: str,
        top_k: int = 5,
        filter_metadata: dict[str, Any] | None = None,
    ) -> VectorSearchResult:
        if self._pool is None:
            logger.warning(
                "vectordb: 后端已降级，search 返回空结果（%s）", self._degraded_reason
            )
            return VectorSearchResult(documents=[])

        vector = await self._embed_one(query)
        if vector is None:
            return VectorSearchResult(documents=await self._keyword_search(query, top_k, filter_metadata))

        candidates = await self._vector_candidates(vector, top_k, filter_metadata)
        if not candidates:
            return VectorSearchResult(documents=await self._keyword_search(query, top_k, filter_metadata))

        query_lower = query.lower()
        pool_best = max(keyword_score(doc.content, query_lower) for doc in candidates)

        # 无向量的行对向量腿不可见，用关键词腿补齐，避免它们永远检索不到。
        # 注意：补齐只在候选池不足 top_k 时触发（即表很小），大表下仍可能漏掉
        # 写入时 embedding 恰好失败的个别行——这是已知的部分缓解，不是完备保证。
        topped_up: list[VectorDocument] = []
        if len(candidates) < top_k:
            topped_up = await self._keyword_search(
                query,
                top_k - len(candidates),
                filter_metadata,
                exclude={doc.id for doc in candidates},
            )
            if topped_up:
                # 归一化基准必须覆盖全部候选，否则两条腿的分数不在同一量纲上。
                pool_best = max(pool_best, max(doc.score for doc in topped_up))

        for doc in candidates:
            doc.score = fuse(
                doc.score, normalized_keyword_score(doc.content, query_lower, pool_best)
            )
        for doc in topped_up:
            # 补齐行没有 embedding，余弦腿贡献 0，只剩归一化的关键词分。
            doc.score = fuse(0.0, normalized_keyword_score(doc.content, query_lower, pool_best))

        results = candidates + topped_up
        # 统一排序：融合分在 0..1，而关键词原始分是无上界的"命中次数"，
        # 不重排的话 documents 就不是按相关度降序，取 documents[0] 会拿到错的结果。
        results.sort(key=lambda doc: doc.score, reverse=True)
        return VectorSearchResult(documents=results[:top_k])

    async def _vector_candidates(
        self, vector: Sequence[float], top_k: int, filter_metadata: dict[str, Any] | None
    ) -> list[VectorDocument]:
        literal = _vector_literal(vector)
        limit = max(top_k * CANDIDATE_MULTIPLIER, MIN_CANDIDATES)
        rows = await self._run(
            f"SELECT id, content, metadata, 1 - (embedding <=> %s::vector) AS score "  # nosec B608 - 表名已校验
            f"FROM {self._table} "
            f"WHERE embedding IS NOT NULL AND metadata @> %s::jsonb AND {_SEARCHABLE_SQL} "
            f"ORDER BY embedding <=> %s::vector "
            f"LIMIT %s",
            (
                literal,
                json.dumps(filter_metadata or {}, ensure_ascii=False),
                literal,
                limit,
            ),
            fetch=True,
        )
        return [
            VectorDocument(id=row[0], content=row[1], metadata=row[2] or {}, score=float(row[3]))
            for row in rows or []
        ]

    async def _keyword_search(
        self,
        query: str,
        top_k: int,
        filter_metadata: dict[str, Any] | None,
        exclude: set[str] | None = None,
    ) -> list[VectorDocument]:
        if top_k <= 0:
            return []
        rows = await self._run(
            f"SELECT id, content, metadata FROM {self._table} "  # nosec B608 - 表名已校验
            f"WHERE metadata @> %s::jsonb AND {_SEARCHABLE_SQL} "
            f"LIMIT %s",
            (
                json.dumps(filter_metadata or {}, ensure_ascii=False),
                self._keyword_scan_limit,
            ),
            fetch=True,
        )
        rows = rows or []
        if len(rows) >= self._keyword_scan_limit:
            logger.warning(
                "vectordb: 关键词回退扫描触及上限 %d 行，结果可能不完整；"
                "请配置 EMBEDDING_API_KEY 以启用向量检索",
                self._keyword_scan_limit,
            )

        query_lower = query.lower()
        skip = exclude or set()
        scored: list[VectorDocument] = []
        for row_id, content, metadata in rows:
            if row_id in skip:
                continue
            score = keyword_score(content, query_lower)
            if score > 0:
                scored.append(
                    VectorDocument(id=row_id, content=content, metadata=metadata or {}, score=score)
                )
        scored.sort(key=lambda doc: doc.score, reverse=True)
        return scored[:top_k]

    # ── embeddings ─────────────────────────────────────────────────────────

    async def _embed_one(self, text: str) -> list[float] | None:
        if not text:
            return None
        vectors = await self._embed_batch([text])
        return vectors[0] if vectors else None

    async def _embed_batch(self, texts: list[str]) -> list[list[float] | None]:
        provider = self._embedding
        if provider is None or not getattr(provider, "enabled", False):
            return [None] * len(texts)
        try:
            vectors = await provider.embed_batch(texts)
        except Exception as exc:  # noqa: BLE001 - provider is best-effort
            logger.warning("vectordb: embedding 调用失败，本次走关键词回退: %s", exc)
            return [None] * len(texts)

        # 长度不一致是 provider 的 bug（少返回向量）。不能用 zip 静默截断
        # ——那样会丢文档且不报错。宁可降级为全 None（文档仍落库，只是无向量）。
        if len(vectors) != len(texts):
            logger.error(
                "vectordb: embedding 返回 %d 条，期望 %d 条；本次写入不带向量",
                len(vectors),
                len(texts),
            )
            return [None] * len(texts)

        accepted: list[list[float] | None] = []
        for vector in vectors:
            if vector is None:
                accepted.append(None)
            elif len(vector) != self._dim:
                logger.warning(
                    "vectordb: embedding 维度 %d 与表定义 %d 不符，丢弃该向量",
                    len(vector),
                    self._dim,
                )
                accepted.append(None)
            else:
                accepted.append(vector)
        return accepted


__all__ = [
    "PgVectorClient",
    "VectorStoreUnavailable",
    "fuse",
    "render_schema",
    "split_statements",
]
