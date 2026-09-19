import pytest

from app.config import Settings, settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "MOA_ENV",
        "REDIS_URL",
        "REDIS_SENTINEL_HOSTS",
        "REDIS_SENTINEL_MASTER",
        "REDIS_ENABLE_FALLBACK",
        "ROUTER_LLM_TIMEOUT_MS",
        "MICRO_LLM_TIMEOUT_MS",
        "HITL_ENABLED",
        "VECTOR_DB_DSN",
        "CODE_REVIEW_DATABASE_URL",
        "VECTOR_DB_EMBEDDING_DIM",
        "CODE_REVIEW_EMBEDDING_DIM",
        "EMBEDDING_API_KEY",
        "CODE_REVIEW_EMBEDDING_API_KEY",
        "EMBEDDING_BASE_URL",
        "CODE_REVIEW_EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "CODE_REVIEW_EMBEDDING_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_defaults():
    s = Settings()
    assert s.env == "dev"
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.redis_sentinel_hosts == []
    assert s.redis_sentinel_master == "mymaster"
    assert s.redis_enable_fallback is True
    assert s.router_llm_timeout_ms == 2000
    assert s.micro_llm_timeout_ms == 1000
    assert s.hitl_enabled is False


def test_env_override(monkeypatch):
    monkeypatch.setenv("MOA_ENV", "prod")
    monkeypatch.setenv("REDIS_URL", "redis://cache.example.com:6380/2")
    monkeypatch.setenv("REDIS_SENTINEL_MASTER", "primary")
    monkeypatch.setenv("ROUTER_LLM_TIMEOUT_MS", "5000")
    monkeypatch.setenv("MICRO_LLM_TIMEOUT_MS", "750")
    s = Settings()
    assert s.env == "prod"
    assert s.redis_url == "redis://cache.example.com:6380/2"
    assert s.redis_sentinel_master == "primary"
    assert s.router_llm_timeout_ms == 5000
    assert s.micro_llm_timeout_ms == 750


def test_sentinel_hosts_parsing(monkeypatch):
    monkeypatch.setenv("REDIS_SENTINEL_HOSTS", "a:26379,b:26379")
    assert Settings().redis_sentinel_hosts == [("a", 26379), ("b", 26379)]


def test_sentinel_hosts_empty(monkeypatch):
    monkeypatch.setenv("REDIS_SENTINEL_HOSTS", "")
    assert Settings().redis_sentinel_hosts == []


def test_redis_enable_fallback_truthy(monkeypatch):
    monkeypatch.setenv("REDIS_ENABLE_FALLBACK", "1")
    assert Settings().redis_enable_fallback is True
    monkeypatch.setenv("REDIS_ENABLE_FALLBACK", "true")
    assert Settings().redis_enable_fallback is True
    monkeypatch.setenv("REDIS_ENABLE_FALLBACK", "TRUE")
    assert Settings().redis_enable_fallback is True


def test_redis_enable_fallback_falsy(monkeypatch):
    monkeypatch.setenv("REDIS_ENABLE_FALLBACK", "0")
    assert Settings().redis_enable_fallback is False
    monkeypatch.setenv("REDIS_ENABLE_FALLBACK", "false")
    assert Settings().redis_enable_fallback is False


def test_hitl_enabled_parsing(monkeypatch):
    assert Settings().hitl_enabled is False
    monkeypatch.setenv("HITL_ENABLED", "1")
    assert Settings().hitl_enabled is True
    monkeypatch.setenv("HITL_ENABLED", "true")
    assert Settings().hitl_enabled is True
    monkeypatch.setenv("HITL_ENABLED", "0")
    assert Settings().hitl_enabled is False


def test_router_llm_timeout_env(monkeypatch):
    monkeypatch.setenv("ROUTER_LLM_TIMEOUT_MS", "1500")
    assert Settings().router_llm_timeout_ms == 1500


def test_micro_llm_timeout_env(monkeypatch):
    monkeypatch.setenv("MICRO_LLM_TIMEOUT_MS", "300")
    assert Settings().micro_llm_timeout_ms == 300


def test_to_redis_config():
    s = Settings()
    assert s.to_redis_config() == {
        "url": s.redis_url,
        "sentinel_hosts": s.redis_sentinel_hosts,
        "sentinel_master": s.redis_sentinel_master,
        "enable_fallback": s.redis_enable_fallback,
    }


def test_settings_singleton():
    assert isinstance(settings, Settings)


def test_vector_settings_accept_code_review_aliases(monkeypatch):
    monkeypatch.setenv(
        "CODE_REVIEW_DATABASE_URL",
        "postgresql://gateway:gateway@localhost:5433/gateway",
    )
    monkeypatch.setenv("CODE_REVIEW_EMBEDDING_DIM", "768")
    monkeypatch.setenv("CODE_REVIEW_EMBEDDING_API_KEY", "ollama-local")
    monkeypatch.setenv(
        "CODE_REVIEW_EMBEDDING_BASE_URL",
        "http://localhost:11434/v1",
    )
    monkeypatch.setenv("CODE_REVIEW_EMBEDDING_MODEL", "nomic-embed-text:latest")

    s = Settings()

    assert s.vector_db_dsn == "postgresql://gateway:gateway@localhost:5433/gateway"
    assert s.vector_db_embedding_dim == 768
    assert s.embedding_api_key == "ollama-local"
    assert s.embedding_base_url == "http://localhost:11434/v1"
    assert s.embedding_model == "nomic-embed-text:latest"
