from __future__ import annotations

from app.deps import build_intent_router


def _clear_env(monkeypatch) -> None:
    for key in (
        "MICRO_LLM_MODEL",
        "MICRO_LLM_API_KEY",
        "ROUTER_LLM_MODEL",
        "ROUTER_LLM_API_KEY",
        "OPENAI_API_KEY",
        "OMNIROUTE_API_KEY",
        "LLM_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_no_keys_keeps_regex_only(monkeypatch) -> None:
    _clear_env(monkeypatch)
    router = build_intent_router()
    assert router.micro_llm is None
    assert router.router_llm is None


def test_micro_llm_wired_when_configured(monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("MICRO_LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("MICRO_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("MICRO_LLM_BASE_URL", "http://localhost:9/v1")
    router = build_intent_router()
    assert router.micro_llm is not None
    assert router.router_llm is None


def test_router_llm_falls_back_to_main_llm(monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    router = build_intent_router()
    assert router.router_llm is not None
    assert router.micro_llm is None
