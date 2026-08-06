from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true"}


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

    def to_redis_config(self) -> dict[str, Any]:
        return {
            "url": self.redis_url,
            "sentinel_hosts": self.redis_sentinel_hosts,
            "sentinel_master": self.redis_sentinel_master,
            "enable_fallback": self.redis_enable_fallback,
        }


settings = Settings()
