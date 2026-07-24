from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from app.prompt_registry import PromptEntry, PromptRegistry

logger = logging.getLogger("moa.prompt_registry.canary")


@dataclass
class CanaryConfig:
    """Configuration for canary traffic splitting."""

    enabled: bool = False
    traffic_pct: int = 10  # percentage of traffic directed to canary
    stable_version: str = "stable"
    canary_version: str = "canary"


def _hash_session(session_id: str) -> int:
    """Deterministic hash of session_id in [0, 100)."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def select_canary_version(
    session_id: str,
    registry: PromptRegistry,
    agent_name: str,
    config: CanaryConfig | None = None,
) -> tuple[PromptEntry, str]:
    """Select prompt version based on canary traffic split.

    Returns (entry, selected_version).
    """
    cfg = config or CanaryConfig()

    if cfg.enabled and _hash_session(session_id) < cfg.traffic_pct:
        try:
            entry = registry.get(agent_name, cfg.canary_version)
            if entry is None:
                raise KeyError(f"canary version {cfg.canary_version} not found")
            logger.debug("canary session=%s agent=%s version=%s", session_id, agent_name, cfg.canary_version)
            return entry, cfg.canary_version
        except KeyError:
            logger.warning("canary version %s not found for %s, falling back to stable", cfg.canary_version, agent_name)

    # Stable group.
    entry = registry.get_or_default(agent_name, cfg.stable_version)
    logger.debug("stable session=%s agent=%s version=%s", session_id, agent_name, cfg.stable_version)
    return entry, cfg.stable_version


__all__ = ["CanaryConfig", "select_canary_version"]
