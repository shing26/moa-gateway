from __future__ import annotations

from typing import Any


class Settings:
    def __init__(self) -> None:
        self.env: str = "dev"
        self.redis_url: str = "redis://localhost:6379/0"
        self.redis_sentinel_hosts: list[tuple[str, int]] = []
        self.redis_sentinel_master: str = "mymaster"
        self.redis_enable_fallback: bool = True
        self.router_llm_timeout_ms: int = 2000
        self.micro_llm_timeout_ms: int = 1000
        self.hitl_enabled: bool = False

    def to_redis_config(self) -> dict[str, Any]:
        return {
            "url": self.redis_url,
            "sentinel_hosts": self.redis_sentinel_hosts,
            "sentinel_master": self.redis_sentinel_master,
            "enable_fallback": self.redis_enable_fallback,
        }


settings = Settings()
