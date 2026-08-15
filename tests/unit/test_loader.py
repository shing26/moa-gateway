from __future__ import annotations

from app.agents import loader  # noqa: F401  triggers agent registration
from app.agents.contract import get_agent
from app.command_mode import parse_command


def test_loader_registers_review_agent() -> None:
    assert get_agent("review") is not None


def test_review_commands_map_to_review_intent() -> None:
    assert parse_command("/review") == ("review", "PR 审查")
    assert parse_command("/审查") == ("review", "PR 审查")
