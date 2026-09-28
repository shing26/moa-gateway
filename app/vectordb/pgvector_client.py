"""PostgreSQL + pgvector backend for the gateway retrieval store.

Interface parity
----------------
This class is a drop-in replacement for the in-memory ``VectorDBClient``:
same coroutine signatures, same ``VectorDocument`` / ``VectorSearchResult``
types, so ``ContextRetriever`` and ``KnowledgeBase`` never learn which backend
they are talking to. Two deliberate exceptions:

* ``count`` is a coroutine here (``acount()``) because it needs a round trip.
  The in-memory client still exposes a synchronous property for its tests.
* ``search`` runs two independent recall legs and fuses them (see below).

Retrieval: two legs + RRF (ADR-020)
-----------------------------------
Vector similarity alone is weak on exact identifiers — error codes, config
keys, proper nouns — where lexical matching is strong, and vice versa. So
``search`` runs two **independent** recall legs and fuses their rankings:

* **dense** — HNSW over ``embedding`` (cosine distance);
* **sparse** — ``ts_rank`` over the pre-tokenised ``tokens`` column, matched by
  ``to_tsvector('simple', tokens) @@ to_tsquery('simple', …)``.

Fusion is Reciprocal Rank Fusion, ``sum(1 / (RRF_K + rank))``. Rank-only fusion
is deliberate. The previous implementation fused *scores*
(``0.7 * cosine + 0.3 * normalised_keyword``) after min-max normalising the
keyword term **against the dense candidate pool** — so the ordering depended on
the pool, and the "keyword leg" could only ever reorder documents the dense leg
had already found: a lexically-matching document outside the dense top-k was
unreachable. RRF removes both problems at once — no scale to calibrate, and a
document the dense leg never retrieved can still surface on the sparse leg.

The ``tokens`` column exists because Postgres' default text search does not
segment Chinese: tokenising ``content`` directly makes Chinese queries match
nothing, **silently**. Tokens come from the *same* ``query_tokens`` function on
the write and the query path, so the two cannot drift apart.

``tokens`` uses the ``simple`` configuration (no stemming) because the column
already holds bigrams and stemming would break them. The index is built on the
**two-argument** ``to_tsvector('simple', tokens)``: only that form is IMMUTABLE
and therefore indexable.

When no embedding provider is configured (or it is circuit-broken, or returns
the wrong dimensionality) the dense leg simply contributes nothing and the
sparse leg carries the query. If *both* legs come back empty we fall back to the
bounded lexical scan — *correct but degraded*, not a stub. That scan has a row
cap (``keyword_scan_limit``) because it is O(N); hitting the cap logs a warning
rather than silently truncating, because a silent wrong answer is worse than a
slow one.

Startup **fails fast** when the configured embedding dimension disagrees with
the table's ``vector(N)``. Continuing would drop every vector (the old behaviour
logged a warning and stored NULL) and quietly degrade retrieval to keyword-only
with no visible symptom.

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
SQL stays flagged and each site remains individually auditable.
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
from app.vectordb.keywords import keyword_score, tokenize_for_index, tsquery_text

logger = logging.getLogger("moa.vectordb.postgres")

# 用 fullmatch 配合无锚点模式，而不是 `^...$` + match：Python 正则里 `$`
# 还会匹配结尾换行符之前的位置，所以 `^...$` 会放过 "gateway_documents\n"。
# 这里是 8 处 B608 抑制标记所依赖的校验，必须严格。
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[2] / "db" / "gateway_schema.sql"

# Reciprocal Rank Fusion 的平滑常数（ADR-020）。RRF 只用**名次**，因此免量纲、免调参
# ——这正是它取代原先 `0.7*cosine + 0.3*归一化关键词` 的理由：那个加权和依赖一个池内
# min-max 归一化基准（pool_best），基准一变排序就跟着变。
RRF_K = 60

# 每条腿各取多少候选。比 top_k 宽，融合才有东西可排；有上限所以大表也不贵。
CANDIDATE_MULTIPLIER = 4
MIN_CANDIDATES = 20

# 启动时回填 tokens 的批量上限（ADR-020）。老库的既有行没有这个列，不回填就永远
# 不会被稀疏腿看见——那是"静默漏召回"。回填分批做并在日志里留下余量，别让一次
# 启动被大表拖死。
_TOKEN_BACKFILL_LIMIT = 5000

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


def rrf_scores(rankings: list[list[str]]) -> dict[str, float]:
    """Reciprocal Rank Fusion：把若干条**已按名次排好**的 id 序列融成一张分数表。

    ``sum(1 / (RRF_K + rank))``，rank 从 1 起。同一文档被两条腿都召回时分数累加，
    所以"两个信号都认为相关"的文档自然排在只有一条腿支持的文档之前——不需要在
    两条腿的分数量纲之间做任何校准。
    """
    totals: dict[str, float] = {}
    for ranking in rankings:
        for position, doc_id in enumerate(ranking, start=1):
            totals[doc_id] = totals.get(doc_id, 0.0) + 1.0 / (RRF_K + position)
    return totals


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
        # embedding provider 返回的向量维度与表定义不符、被丢弃的条数。>0 意味着
        # **语义索引实际上没在建立**：稠密腿永远是空的，检索静默退化成纯稀疏。
        # 启动时的表维度校验抓不到这一种（那里比的是配置 vs 建表，两边都对，错的是模型），
        # 所以只能在这里计数 + 报错，并让它出现在 describe()/healthz 上。
        self._dim_mismatches = 0

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
            # >0 = 模型返回的维度与表不符，向量被逐条丢弃、语义索引没在建立。
            "embedding_dim_mismatches": self._dim_mismatches,
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
        # 维度校验放在"启动不炸"的 except **之外**：故意让它能逃出去。配置与建表维度
        # 不一致时，继续跑等于每条向量都被静默丢弃、检索退化成纯关键词（ADR-020 决策 4）。
        await self._assert_dim_matches()
        # 回填是 best-effort：失败只让稀疏腿不完整，不该拖垮启动。
        await self._backfill_tokens()
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
                # 稀疏腿的预分词列（ADR-020）：写入与查询必须用同一个分词函数。
                tokenize_for_index(doc.content),
            )
            for doc, vec in zip(docs, vectors)
        ]
        # COALESCE on the conflict path matters: if the embedding call failed
        # transiently we must not overwrite a previously stored good vector
        # with NULL, or an outage would silently erase the semantic index.
        await self._run_many(
            f"INSERT INTO {self._table} (id, content, metadata, embedding, tokens) "  # nosec B608 - 表名已校验
            f"VALUES (%s, %s, %s::jsonb, %s::vector, %s) "
            f"ON CONFLICT (id) DO UPDATE SET "
            f"content = EXCLUDED.content, "
            f"metadata = EXCLUDED.metadata, "
            f"embedding = COALESCE(EXCLUDED.embedding, {self._table}.embedding), "
            f"tokens = EXCLUDED.tokens, "
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

        # 稠密腿：向量召回（HNSW，按余弦距离排序）。
        dense: list[VectorDocument] = []
        vector = await self._embed_one(query)
        if vector is not None:
            dense = await self._vector_candidates(vector, top_k, filter_metadata)

        # 稀疏腿：**独立召回**（tsvector + ts_rank）。它不是"在稠密候选池里重排"
        # ——那正是此前"混合检索"名不副实的地方：词法命中但排在稠密 top-k 之外的
        # 文档永远召不回（ADR-020 背景）。
        sparse = await self._sparse_candidates(query, top_k, filter_metadata)

        if not dense and not sparse:
            # 两条腿都没召回到东西：退回有界的词法扫描。表极小、或 tokens 尚未回填
            # 完时会走到这里——它是**部分**缓解，不是完备保证。
            return VectorSearchResult(
                documents=await self._keyword_search(query, top_k, filter_metadata)
            )

        by_id: dict[str, VectorDocument] = {doc.id: doc for doc in (*dense, *sparse)}
        rankings: list[list[str]] = [[doc.id for doc in dense], [doc.id for doc in sparse]]

        # 无向量、又没回填 tokens 的行两条腿都看不见。稠密候选不足 top_k 时补一次
        # 有界词法扫描，把它作为**第三条腿**交给 RRF（而不是像以前那样再自己算一套
        # 加权分——那正是量纲要小心处理的地方）。
        if len(dense) < top_k:
            lexical = await self._keyword_search(
                query, top_k, filter_metadata, exclude=set(by_id)
            )
            for doc in lexical:
                by_id.setdefault(doc.id, doc)
            rankings.append([doc.id for doc in lexical])

        fused = rrf_scores(rankings)
        ranked = list(by_id.values())
        for doc in ranked:
            doc.score = fused.get(doc.id, 0.0)
        # 融合分并列很常见（只被一条腿召回、名次又相同），必须给次级键，
        # 否则返回顺序不稳定。
        ranked.sort(key=lambda doc: (-doc.score, doc.id))
        return VectorSearchResult(documents=ranked[:top_k])

    async def _sparse_candidates(
        self,
        query: str,
        top_k: int,
        filter_metadata: dict[str, Any] | None,
    ) -> list[VectorDocument]:
        """稀疏腿：从预分词列按 ``ts_rank`` 召回（ADR-020 决策 1）。

        分词走 ``query_tokens``（经 ``tsquery_text`` 拼成 OR 表达式），与写入侧
        ``tokenize_for_index`` 是**同一个函数**——两份分词实现迟早会漂移。
        用 ``|`` 而非 AND：bigram 做 AND 太严，中文长查询会让召回塌掉。
        """
        if top_k <= 0 or not query:
            return []
        query_ts = tsquery_text(query.lower())
        if not query_ts:
            return []
        limit = max(top_k * CANDIDATE_MULTIPLIER, MIN_CANDIDATES)
        rows = await self._run(
            f"SELECT id, content, metadata, "  # nosec B608 - 表名已校验
            f"ts_rank(to_tsvector('simple', tokens), to_tsquery('simple', %s)) AS score "
            f"FROM {self._table} "
            f"WHERE tokens IS NOT NULL AND metadata @> %s::jsonb AND {_SEARCHABLE_SQL} "
            f"AND to_tsvector('simple', tokens) @@ to_tsquery('simple', %s) "
            f"ORDER BY score DESC, id LIMIT %s",
            (
                query_ts,
                json.dumps(filter_metadata or {}, ensure_ascii=False),
                query_ts,
                limit,
            ),
            fetch=True,
        )
        return [
            VectorDocument(
                id=row[0], content=row[1], metadata=row[2] or {}, score=float(row[3])
            )
            for row in rows or []
        ]

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

    async def _assert_dim_matches(self) -> None:
        """校验表里 embedding 的实际维度与配置一致；不一致**直接拒绝启动**。

        旧行为是 `_embed_batch` 对每条不符的向量打一条 warn 然后丢弃——于是配置与建表
        维度不一致时，**整库向量逐条丢失、检索静默退化成纯关键词**。这个错误没有任何
        外显症状，只能靠这里当场拦住（ADR-020 决策 4）。
        """
        rows = await self._run(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = to_regclass(%s) AND attname = 'embedding'",
            (self._table,),
            fetch=True,
        )
        if not rows or not rows[0][0]:
            return  # 表还没建好：迁移会按配置维度建，无需比对
        match = re.search(r"vector\((\d+)\)", str(rows[0][0]))
        if match is None:
            return
        actual = int(match.group(1))
        if actual != self._dim:
            raise VectorStoreUnavailable(
                f"表 {self._table} 的 embedding 维度是 {actual}，配置是 {self._dim}。"
                f"不一致会让每条向量被静默丢弃、检索退化为纯关键词。"
                f"请对齐 VECTOR_DB_EMBEDDING_DIM 与建表维度，或重建该列与索引。"
            )

    async def _backfill_tokens(self) -> None:
        """给老库回填 `tokens`（ADR-020）。分批、best-effort，余量留在日志里。

        不回填的后果是**静默漏召回**：那批行永远不被稀疏腿看见。单次上限是为了不让
        一次启动被大表拖死——余量会在下次启动继续回填，日志会说明还有。
        """
        try:
            rows = await self._run(
                f"SELECT id, content FROM {self._table} "  # nosec B608 - 表名已校验
                f"WHERE tokens IS NULL LIMIT %s",
                (_TOKEN_BACKFILL_LIMIT,),
                fetch=True,
            )
            rows = rows or []
            if not rows:
                return
            await self._run_many(
                f"UPDATE {self._table} SET tokens = %s WHERE id = %s",  # nosec B608 - 表名已校验
                [(tokenize_for_index(content or ""), doc_id) for doc_id, content in rows],
            )
            logger.info("vectordb: 回填 tokens %d 行", len(rows))
            if len(rows) >= _TOKEN_BACKFILL_LIMIT:
                logger.warning(
                    "vectordb: tokens 回填触及单次上限 %d，仍有未回填的行——"
                    "这些行此刻只可能被稠密腿召回（重启会继续回填）",
                    _TOKEN_BACKFILL_LIMIT,
                )
        except Exception as exc:  # noqa: BLE001 - 回填是 best-effort，不该拖垮启动
            logger.warning("vectordb: tokens 回填失败，稀疏腿本次不完整: %s", exc)

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
                # 提升为 error + 计数（2026-09-28）。此前只有一条 warning 然后丢弃：
                # 若模型换了而上限没跟着改，**每一条**向量都会走到这里 → 稠密腿永远空
                # → 检索静默退化成纯稀疏，而表现与"没配 embedding"几乎一样。
                # 启动时的表维度校验抓不到它（配置与表一致，错的是模型），所以这里必须响。
                self._dim_mismatches += 1
                logger.error(
                    "vectordb: embedding 维度 %d 与表定义 %d 不符，丢弃该向量"
                    "（累计 %d 条）。语义索引没有在建立——请对齐 EMBEDDING_MODEL 与 "
                    "VECTOR_DB_EMBEDDING_DIM，或重建该列与索引。",
                    len(vector),
                    self._dim,
                    self._dim_mismatches,
                )
                accepted.append(None)
            else:
                accepted.append(vector)
        return accepted


__all__ = [
    "PgVectorClient",
    "VectorStoreUnavailable",
    "rrf_scores",
    "render_schema",
    "split_statements",
]
