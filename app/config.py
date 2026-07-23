from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    env: str = "dev"
    redis_url: str = "redis://localhost:6379/0"
    router_llm_timeout_ms: int = 2000
    micro_llm_timeout_ms: int = 1000
    hitl_enabled: bool = False

    class Config:
        env_prefix = ""
        case_sensitive = False


settings = Settings()
