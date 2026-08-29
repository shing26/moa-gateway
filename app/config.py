from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true"}


def _parse_int(value: str | None, default: int) -> int:
    """Parse an int, falling back to ``default`` on garbage.

    Deliberately forgiving: a malformed env var should not stop the gateway
    from booting. The timeout settings above parse with bare ``int()`` and will
    raise on bad input; new settings use this instead.
    """
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _parse_float(value: str | None, default: float) -> float:
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


def _parse_es_hosts(value: str | None) -> list[str]:
    if not value:
        return []
    hosts: list[str] = []
    for item in value.split(","):
        item = item.strip()
        if item:
            hosts.append(item)
    return hosts


def _parse_sentinel_hosts(value: str | None) -> list[tuple[str, int]]:
    if not value:
        return []
    hosts: list[tuple[str, int]] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 1)
        if len(parts) != 2:
            continue
        host, port = parts
        try:
            hosts.append((host.strip(), int(port)))
        except ValueError:
            continue
    return hosts


class Settings:
    def __init__(self) -> None:
        self.env: str = os.getenv("MOA_ENV", "dev")
        self.redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.redis_sentinel_hosts: list[tuple[str, int]] = _parse_sentinel_hosts(
            os.getenv("REDIS_SENTINEL_HOSTS")
        )
        self.redis_sentinel_master: str = os.getenv("REDIS_SENTINEL_MASTER", "mymaster")
        self.redis_enable_fallback: bool = _parse_bool(os.getenv("REDIS_ENABLE_FALLBACK"), True)
        self.router_llm_timeout_ms: int = int(os.getenv("ROUTER_LLM_TIMEOUT_MS", "2000"))
        self.micro_llm_timeout_ms: int = int(os.getenv("MICRO_LLM_TIMEOUT_MS", "1000"))
        self.hitl_enabled: bool = _parse_bool(os.getenv("HITL_ENABLED"), False)
        self.feishu_verification_token: str = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
        self.feishu_encrypt_key: str = os.getenv("FEISHU_ENCRYPT_KEY", "")
        self.es_hosts: list[str] = _parse_es_hosts(os.getenv("ES_HOSTS", ""))
        self.es_index_prefix: str = os.getenv("ES_INDEX_PREFIX", "moa-audit")

        # ── 检索存储 (P4 / P5) ─────────────────────────────────────────────
        # 空 DSN = 内存存储，即当前行为，零变化；配置 DSN 后由 PostgreSQL +
        # pgvector 接管，上层消费者无需改动。
        self.vector_db_dsn: str = os.getenv("VECTOR_DB_DSN", "")
        self.vector_db_table: str = os.getenv("VECTOR_DB_TABLE", "gateway_documents")
        # 维度必须与 db/gateway_schema.sql 中的 vector(N) 一致。
        self.vector_db_embedding_dim: int = _parse_int(os.getenv("VECTOR_DB_EMBEDDING_DIM"), 1536)
        self.vector_db_pool_min_size: int = _parse_int(os.getenv("VECTOR_DB_POOL_MIN_SIZE"), 1)
        self.vector_db_pool_max_size: int = _parse_int(os.getenv("VECTOR_DB_POOL_MAX_SIZE"), 4)
        self.vector_db_keyword_scan_limit: int = _parse_int(
            os.getenv("VECTOR_DB_KEYWORD_SCAN_LIMIT"), 2000
        )
        # strict=False：PG 不可用时降级并在 /healthz 暴露，不阻断启动。
        # 拿到稳定实例后建议置 1，让配置错误在启动阶段就暴露。
        self.vector_db_strict: bool = _parse_bool(os.getenv("VECTOR_DB_STRICT"), False)
        self.vector_db_auto_migrate: bool = _parse_bool(os.getenv("VECTOR_DB_AUTO_MIGRATE"), True)

        # ── Embedding ─────────────────────────────────────────────────────
        # 未配置则整条语义检索关闭，走 BD-01 关键词回退。仅在配置了
        # VECTOR_DB_DSN 时才会真正发起调用，因此不会影响现有的内存存储路径。
        self.embedding_api_key: str = os.getenv("EMBEDDING_API_KEY", "") or os.getenv(
            "OPENAI_API_KEY", ""
        )
        self.embedding_base_url: str = (
            os.getenv("EMBEDDING_BASE_URL", "")
            or os.getenv("OPENAI_BASE_URL", "")
            or "https://api.openai.com/v1"
        )
        self.embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        self.embedding_timeout_s: float = _parse_float(os.getenv("EMBEDDING_TIMEOUT_S"), 10.0)

    def to_redis_config(self) -> dict[str, Any]:
        return {
            "url": self.redis_url,
            "sentinel_hosts": self.redis_sentinel_hosts,
            "sentinel_master": self.redis_sentinel_master,
            "enable_fallback": self.redis_enable_fallback,
        }


settings = Settings()
