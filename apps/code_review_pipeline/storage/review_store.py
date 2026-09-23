from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("moa.code_review.storage")


class StorageInitError(Exception):
    """Raised when the persistent store cannot be initialized."""


def _missing_deps() -> list[str]:
    missing = []
    try:
        import psycopg  # noqa: F401
    except Exception:
        missing.append("psycopg")
    try:
        import pydantic  # noqa: F401
    except Exception:
        missing.append("pydantic")
    return missing


def _build_dsn() -> str | None:
    """DSN 与 ``app.config.settings`` 同源（同类分叉的第三例，2026-09-23）。

    此前这里读 ``CODE_REVIEW_DATABASE_URL / DATABASE_URL / POSTGRES_URL``，而 config
    读 ``VECTOR_DB_DSN / CODE_REVIEW_DATABASE_URL`` —— **只设 ``VECTOR_DB_DSN`` 的部署
    在网关侧"有库"，在这里却拿到 None 而静默回落非持久化**。现在 config 的链已收编
    三组名字，这里只做单点读取。
    """
    from app.config import settings

    return settings.vector_db_dsn or None


def _embedding_dim() -> int:
    """向量维度：与 ``app.config.settings`` 同源。

    回归（2026-09-23）：本函数与 ``rag/embeddings.py`` 的同名函数此前都**反向**
    优先 ``CODE_REVIEW_EMBEDDING_DIM``，而 ``app/config.py`` 优先
    ``VECTOR_DB_EMBEDDING_DIM`` —— 同一组 env 可能算出不同维度，而这个维度决定了
    建表 DDL 与写入向量的长度，错了直接失败。非正整数的 fail-fast 交给 config 层。
    """
    from app.config import settings

    dim = int(settings.vector_db_embedding_dim)
    if dim <= 0:
        raise StorageInitError(f"embedding dimension must be positive: {dim}")
    return dim


def render_schema(schema_sql: str, dim: int) -> str:
    """Bind the configured embedding dimension into the review pgvector DDL."""
    if dim <= 0:
        raise ValueError(f"embedding dimension must be positive: {dim}")
    return schema_sql.replace("vector(1536)", f"vector({dim})")


def _ensure_schema(dsn: str) -> None:
    import psycopg  # type: ignore[import-untyped]

    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as fh:
        schema_sql = render_schema(fh.read(), _embedding_dim())

    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
                conn.commit()
    except Exception as exc:
        raise StorageInitError(f"failed to apply review schema: {exc}") from exc


def _create_pg_store(dsn: str) -> "PostgresReviewStore":
    try:
        import psycopg  # type: ignore[import-untyped]
    except Exception as exc:  # pragma: no cover
        raise StorageInitError(f"psycopg is required for Postgres store: {exc}") from exc
    return PostgresReviewStore(dsn=dsn, _psycopg=psycopg)


@dataclass(frozen=True)
class ReviewRecord:
    trace_id: str
    repo: str
    pr_number: int
    head_sha: str
    author: str
    findings_count: int
    need_human_review: bool
    raw: dict[str, Any]


class ReviewStore:
    def __init__(self) -> None:
        self._records: dict[str, ReviewRecord] = {}

    def save(self, record: ReviewRecord) -> None:
        self._records[record.trace_id] = record
        logger.info("review saved trace=%s findings=%d", record.trace_id, record.findings_count)

    def get(self, trace_id: str) -> ReviewRecord | None:
        return self._records.get(trace_id)

    async def close(self) -> None:
        self._records.clear()


class PostgresReviewStore:
    def __init__(self, dsn: str, _psycopg: Any | None = None) -> None:
        self._dsn = dsn
        self._psycopg = _psycopg
        self._conn = None

    def _connect(self):
        if self._conn is None:
            if self._psycopg is None:
                import psycopg  # type: ignore[import-untyped]
                self._psycopg = psycopg
            self._conn = self._psycopg.connect(self._dsn)

    def save(self, record: ReviewRecord) -> None:
        self._connect()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO code_review_prs (
                        trace_id, repo, pr_number, head_sha, base_sha, title, author, html_url, diff_url,
                        changed_files_count, labels, reviewers, overall_need_human_review, status
                    ) VALUES (
                        %(trace_id)s, %(repo)s, %(pr_number)s, %(head_sha)s, %(base_sha)s, %(title)s,
                        %(author)s, %(html_url)s, %(diff_url)s, %(changed_files_count)s,
                        %(labels)s, %(reviewers)s, %(overall_need_human_review)s, %(status)s
                    )
                    ON CONFLICT (trace_id) DO UPDATE SET
                        head_sha = EXCLUDED.head_sha,
                        changed_files_count = EXCLUDED.changed_files_count,
                        reviewers = EXCLUDED.reviewers,
                        overall_need_human_review = EXCLUDED.overall_need_human_review,
                        status = EXCLUDED.status,
                        updated_at = NOW()
                    """,
                    {
                        "trace_id": record.trace_id,
                        "repo": record.raw.get("repo", ""),
                        "pr_number": int(record.raw.get("pr_number", 0) or 0),
                        "head_sha": record.raw.get("head_sha", record.head_sha),
                        "base_sha": record.raw.get("base_sha", ""),
                        "title": record.raw.get("title", ""),
                        "author": record.author,
                        "html_url": record.raw.get("html_url", ""),
                        "diff_url": record.raw.get("diff_url", ""),
                        "changed_files_count": int(record.raw.get("changed_files_count", 0) or 0),
                        "labels": record.raw.get("labels", []),
                        "reviewers": record.raw.get("reviewers", []),
                        "overall_need_human_review": bool(record.need_human_review),
                        "status": "pending",
                    },
                )
                self._conn.commit()
        except Exception:
            if self._conn:
                self._conn.rollback()
            raise

    def get(self, trace_id: str) -> ReviewRecord | None:
        self._connect()
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT trace_id, repo, pr_number, head_sha, author, changed_files_count, overall_need_human_review FROM code_review_prs WHERE trace_id = %s", (trace_id,))
                row = cur.fetchone()
                if not row:
                    return None
                trace_id, repo, pr_number, head_sha, author, changed_files_count, overall_need_human_review = row
                return ReviewRecord(
                    trace_id=trace_id,
                    repo=repo,
                    pr_number=int(pr_number or 0),
                    head_sha=head_sha or "",
                    author=author or "",
                    findings_count=int(changed_files_count or 0),
                    need_human_review=bool(overall_need_human_review),
                    raw={},
                )
        except Exception:
            return None

    async def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def build_review_store() -> ReviewStore:
    """
    Build the review store based on environment config.

    Priority:
    1. Postgres if CODE_REVIEW_DATABASE_URL / DATABASE_URL / POSTGRES_URL is set
    2. In-memory fallback otherwise
    """
    dsn = _build_dsn()
    if not dsn:
        logger.info("no database URL configured; using in-memory review store")
        return ReviewStore()

    missing = _missing_deps()
    if missing:
        raise StorageInitError(
            "database URL is configured, but required packages are missing: "
            + ", ".join(missing)
        )

    _ensure_schema(dsn)
    logger.info("using Postgres review store")
    return PostgresReviewStore(dsn=dsn)
