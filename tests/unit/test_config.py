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
        "VECTOR_DB_POOL_MIN_SIZE",
        "VECTOR_DB_POOL_MAX_SIZE",
        "VECTOR_DB_KEYWORD_SCAN_LIMIT",
        "EMBEDDING_TIMEOUT_S",
        "GATEWAY_PORT",
        "APP_PORT",
        "GATEWAY_ALLOW_INSECURE",
        "CODE_REVIEW_MERGE_ENABLED",
        "CODE_REVIEW_MERGE_CHANNEL",
        "DELIVERY_TOPOLOGY",
        "DELIVERY_MAX_STEPS",
        "DELIVERY_MAX_TOKENS",
        "DELIVERY_RUN_RECORD_DIR",
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


# ── fail-fast 校验（validate）与旁路收编字段 ────────────────────────────────


def test_blank_embedding_dim_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("VECTOR_DB_EMBEDDING_DIM", "  ")
    monkeypatch.setenv("CODE_REVIEW_EMBEDDING_DIM", "")
    s = Settings()
    assert s.vector_db_embedding_dim == 1536


def test_invalid_embedding_dim_raises_with_var_name(monkeypatch):
    monkeypatch.setenv("VECTOR_DB_EMBEDDING_DIM", "seven-sixty-eight")
    with pytest.raises(ValueError, match="VECTOR_DB_EMBEDDING_DIM"):
        Settings()


def test_nonpositive_embedding_dim_raises(monkeypatch):
    monkeypatch.setenv("VECTOR_DB_EMBEDDING_DIM", "0")
    with pytest.raises(ValueError, match="VECTOR_DB_EMBEDDING_DIM"):
        Settings()
    monkeypatch.setenv("VECTOR_DB_EMBEDDING_DIM", "-768")
    with pytest.raises(ValueError, match="VECTOR_DB_EMBEDDING_DIM"):
        Settings()


def test_alias_dim_invalid_also_raises(monkeypatch):
    monkeypatch.setenv("CODE_REVIEW_EMBEDDING_DIM", "0")
    with pytest.raises(ValueError, match="VECTOR_DB_EMBEDDING_DIM"):
        Settings()


def test_zero_router_llm_timeout_raises(monkeypatch):
    monkeypatch.setenv("ROUTER_LLM_TIMEOUT_MS", "0")
    with pytest.raises(ValueError, match="ROUTER_LLM_TIMEOUT_MS"):
        Settings()


def test_zero_micro_llm_timeout_raises(monkeypatch):
    monkeypatch.setenv("MICRO_LLM_TIMEOUT_MS", "0")
    with pytest.raises(ValueError, match="MICRO_LLM_TIMEOUT_MS"):
        Settings()


def test_pool_min_greater_than_max_raises(monkeypatch):
    monkeypatch.setenv("VECTOR_DB_POOL_MIN_SIZE", "8")
    monkeypatch.setenv("VECTOR_DB_POOL_MAX_SIZE", "4")
    with pytest.raises(ValueError, match="VECTOR_DB_POOL_MIN_SIZE"):
        Settings()


def test_gateway_port_out_of_range_raises(monkeypatch):
    monkeypatch.setenv("GATEWAY_PORT", "70000")
    with pytest.raises(ValueError, match="GATEWAY_PORT"):
        Settings()
    monkeypatch.setenv("GATEWAY_PORT", "0")
    with pytest.raises(ValueError, match="GATEWAY_PORT"):
        Settings()


def test_gateway_port_blank_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("GATEWAY_PORT", "")
    assert Settings().gateway_port == 8081


def test_auth_fields_read_from_env(monkeypatch):
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", "tok-123")  # nosec B105
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw-123")  # nosec B105
    s = Settings()
    assert s.webhook_auth_token == "tok-123"
    assert s.dashboard_password == "pw-123"
    assert s.gateway_allow_insecure == ""


def test_warn_tier_numeric_garbage_falls_back_with_warning(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("EMBEDDING_TIMEOUT_S", "not-a-number")
    monkeypatch.setenv("VECTOR_DB_KEYWORD_SCAN_LIMIT", "lots")
    with caplog.at_level(logging.WARNING, logger="moa.config"):
        s = Settings()
    assert s.embedding_timeout_s == 10.0
    assert s.vector_db_keyword_scan_limit == 2000
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("EMBEDDING_TIMEOUT_S" in r.getMessage() for r in warnings)
    assert any("VECTOR_DB_KEYWORD_SCAN_LIMIT" in r.getMessage() for r in warnings)


def test_sentinel_garbage_entries_warn_but_boot(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("REDIS_SENTINEL_HOSTS", "a:26379,bad-host,c:nope")
    with caplog.at_level(logging.WARNING, logger="moa.config"):
        s = Settings()
    assert s.redis_sentinel_hosts == [("a", 26379)]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


def test_merge_enabled_without_shared_db_raises(monkeypatch):
    """合并通道由独立 worker 进程轮询，内存实现下两个进程各持一份状态。

    配置成这个组合时通道**永远不会推进**，而且不报错——只是不动。所以要在
    启动时就当作非法配置拒掉，而不是留给人去猜。
    """
    monkeypatch.setenv("CODE_REVIEW_MERGE_ENABLED", "1")
    with pytest.raises(ValueError, match="共享"):
        Settings()


def test_merge_enabled_with_shared_db_boots(monkeypatch):
    monkeypatch.setenv("CODE_REVIEW_MERGE_ENABLED", "1")
    monkeypatch.setenv(
        "CODE_REVIEW_DATABASE_URL", "postgresql://u:p@localhost:5432/gateway"
    )
    assert Settings().code_review_merge_enabled is True


def test_merge_disabled_without_db_still_boots(monkeypatch):
    """默认关闭时不该因为没配库就拒绝启动。"""
    assert Settings().code_review_merge_enabled is False


def test_delivery_defaults_are_single_and_bounded() -> None:
    s = Settings()
    assert s.delivery_topology == "single"
    assert s.delivery_max_steps == 40
    assert s.delivery_max_tokens == 400_000
    assert s.delivery_run_record_dir == "data/delivery-runs"


def test_delivery_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_TOPOLOGY", "verified")
    monkeypatch.setenv("DELIVERY_MAX_STEPS", "12")
    monkeypatch.setenv("DELIVERY_MAX_TOKENS", "9000")
    monkeypatch.setenv("DELIVERY_RUN_RECORD_DIR", "tmp/runs")
    s = Settings()
    assert s.delivery_topology == "verified"
    assert s.delivery_max_steps == 12
    assert s.delivery_max_tokens == 9000
    assert s.delivery_run_record_dir == "tmp/runs"


def test_delivery_rejects_unknown_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_TOPOLOGY", "swarm")
    with pytest.raises(ValueError, match="DELIVERY_TOPOLOGY"):
        Settings()


def test_delivery_rejects_nonpositive_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_MAX_STEPS", "0")
    with pytest.raises(ValueError, match="DELIVERY_MAX_STEPS"):
        Settings()
    monkeypatch.setenv("DELIVERY_MAX_STEPS", "40")
    monkeypatch.setenv("DELIVERY_MAX_TOKENS", "0")
    with pytest.raises(ValueError, match="DELIVERY_MAX_TOKENS"):
        Settings()


def test_ci_triage_defaults_are_read_only_and_bounded() -> None:
    s = Settings()
    assert s.ci_triage_output_dir == "data/ci-triage"
    assert s.ci_triage_max_jobs == 10
    assert s.ci_triage_max_log_chars == 50_000


def test_ci_triage_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI_TRIAGE_OUTPUT_DIR", "tmp/ci-triage")
    monkeypatch.setenv("CI_TRIAGE_MAX_JOBS", "3")
    monkeypatch.setenv("CI_TRIAGE_MAX_LOG_CHARS", "9000")

    s = Settings()

    assert s.ci_triage_output_dir == "tmp/ci-triage"
    assert s.ci_triage_max_jobs == 3
    assert s.ci_triage_max_log_chars == 9000


def test_ci_triage_rejects_nonpositive_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI_TRIAGE_MAX_JOBS", "0")
    with pytest.raises(ValueError, match="CI_TRIAGE_MAX_JOBS"):
        Settings()
    monkeypatch.setenv("CI_TRIAGE_MAX_JOBS", "10")
    monkeypatch.setenv("CI_TRIAGE_MAX_LOG_CHARS", "0")
    with pytest.raises(ValueError, match="CI_TRIAGE_MAX_LOG_CHARS"):
        Settings()
