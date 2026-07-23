from __future__ import annotations


class Settings:
    def __init__(self) -> None:
        self.env = "dev"
        self.redis_url = "redis://localhost:6379/0"
        self.router_llm_timeout_ms = 2000
        self.micro_llm_timeout_ms = 1000
        self.hitl_enabled = False


settings = Settings()
