from __future__ import annotations

import pytest

from app.prompt_registry import PromptEntry, PromptRegistry
from app.prompt_registry.canary import CanaryConfig, select_canary_version


class TestPromptRegistry:
    def test_register_and_get(self):
        registry = PromptRegistry()
        entry = PromptEntry(agent_name="coder", version="v1", system_prompt="You are a coder.")
        registry.register(entry)
        retrieved = registry.get("coder", "v1")
        assert retrieved is not None
        assert retrieved.system_prompt == "You are a coder."

    def test_set_active_and_get_active(self):
        registry = PromptRegistry()
        registry.register(PromptEntry("coder", "v1", "prompt v1"))
        registry.register(PromptEntry("coder", "v2", "prompt v2"))
        registry.set_active("coder", "v2")
        active = registry.get_active("coder")
        assert active is not None
        assert active.version == "v2"

    def test_get_or_default_falls_through(self):
        registry = PromptRegistry()
        registry.register(PromptEntry("general", "default", "default prompt"))
        entry = registry.get_or_default("general")
        assert entry is not None
        assert entry.system_prompt == "default prompt"

    def test_get_or_default_with_version(self):
        registry = PromptRegistry()
        registry.register(PromptEntry("coder", "v3", "prompt v3"))
        entry = registry.get_or_default("coder", "v3")
        assert entry.system_prompt == "prompt v3"

    def test_list_versions(self):
        registry = PromptRegistry()
        registry.register(PromptEntry("coder", "v1", "a"))
        registry.register(PromptEntry("coder", "v2", "b"))
        registry.register(PromptEntry("general", "v1", "c"))
        versions = registry.list_versions("coder")
        assert versions == ["v1", "v2"]

    def test_missing_agent_raises(self):
        registry = PromptRegistry()
        with pytest.raises(KeyError):
            registry.get_or_default("nonexistent")

    def test_entry_count(self):
        registry = PromptRegistry()
        registry.register(PromptEntry("a", "1", ""))
        registry.register(PromptEntry("a", "2", ""))
        registry.register(PromptEntry("b", "1", ""))
        assert registry.entry_count == 3

    def test_set_active_missing_version_raises(self):
        registry = PromptRegistry()
        with pytest.raises(KeyError):
            registry.set_active("coder", "nonexistent")

    def test_key_property(self):
        entry = PromptEntry(agent_name="coder", version="v2.1", system_prompt="x")
        assert entry.key == "prompt:coder:v2.1"


class TestCanary:
    def test_hash_is_deterministic(self):
        from app.prompt_registry.canary import _hash_session
        h1 = _hash_session("session-1")
        h2 = _hash_session("session-1")
        assert h1 == h2

    def test_hash_in_range(self):
        from app.prompt_registry.canary import _hash_session
        for sid in ["a", "b", "c", "session-123", "user_456"]:
            h = _hash_session(sid)
            assert 0 <= h < 100

    def test_disabled_canary_uses_stable(self):
        registry = PromptRegistry()
        registry.register(PromptEntry("coder", "stable", "stable prompt"))
        registry.set_active("coder", "stable")
        config = CanaryConfig(enabled=False, traffic_pct=50)
        entry, version = select_canary_version("any-session", registry, "coder", config)
        assert version == "stable"

    def test_canary_fallback_to_stable(self):
        registry = PromptRegistry()
        registry.register(PromptEntry("coder", "stable", "stable prompt"))
        registry.set_active("coder", "stable")
        config = CanaryConfig(enabled=True, traffic_pct=100, canary_version="canary", stable_version="stable")
        entry, version = select_canary_version("test-session", registry, "coder", config)
        # 100% canary, but no canary prompt registered -> falls back to stable
        assert version == "stable"
