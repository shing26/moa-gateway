from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("moa.prompt_registry")


@dataclass
class PromptEntry:
    agent_name: str
    version: str
    system_prompt: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"prompt:{self.agent_name}:{self.version}"


@dataclass
class PromptRegistry:
    """Versioned prompt storage with in-memory dict backing.

    Key pattern: prompt:{agent_name}:{version}
    Active pointer: prompt:{agent_name}:active  ->  version string
    Fallback: prompts/{agent_name}/default (hardcoded constant)
    """

    _entries: dict[str, PromptEntry] = field(default_factory=dict)
    _active: dict[str, str] = field(default_factory=dict)

    def register(self, entry: PromptEntry) -> None:
        self._entries[entry.key] = entry
        logger.info("prompt registered: %s", entry.key)

    def set_active(self, agent_name: str, version: str) -> None:
        key = f"prompt:{agent_name}:{version}"
        if key not in self._entries:
            raise KeyError(f"prompt not found: {key}")
        self._active[agent_name] = version
        logger.info("prompt active set: %s -> %s", agent_name, version)

    def get_active(self, agent_name: str) -> PromptEntry | None:
        version = self._active.get(agent_name)
        if version is None:
            return None
        return self._entries.get(f"prompt:{agent_name}:{version}")

    def get(self, agent_name: str, version: str) -> PromptEntry | None:
        return self._entries.get(f"prompt:{agent_name}:{version}")

    def get_or_default(self, agent_name: str, version: str | None = None) -> PromptEntry:
        if version:
            entry = self.get(agent_name, version)
            if entry:
                return entry
        active = self.get_active(agent_name)
        if active:
            return active
        # Fallback to default.
        entry = self._entries.get(f"prompt:{agent_name}:default")
        if entry:
            return entry
        raise KeyError(f"no prompt found for agent={agent_name} version={version}")

    def list_versions(self, agent_name: str) -> list[str]:
        prefix = f"prompt:{agent_name}:"
        return sorted(
            k[len(prefix):] for k in self._entries if k.startswith(prefix)
        )

    @property
    def entry_count(self) -> int:
        return len(self._entries)


__all__ = ["PromptEntry", "PromptRegistry"]
