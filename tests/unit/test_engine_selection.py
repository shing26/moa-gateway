"""ENGINE switch selection: default stays FSM, langgraph fails safe."""

from __future__ import annotations

import sys

import pytest

from app import deps


def test_default_engine_keeps_the_fsm_pipeline() -> None:
    assert deps.settings.engine in ("fsm", "langgraph")
    assert deps.settings.engine == "fsm" or hasattr(deps.pipeline, "_graph")


def test_selecting_fsm_returns_the_given_pipeline(monkeypatch) -> None:
    monkeypatch.setattr(deps.settings, "engine", "fsm")
    sentinel = object()
    assert deps._select_orchestrator(sentinel) is sentinel


def test_unknown_engine_falls_back_to_fsm(monkeypatch) -> None:
    monkeypatch.setattr(deps.settings, "engine", "autogen")
    sentinel = object()
    assert deps._select_orchestrator(sentinel) is sentinel


def test_missing_langgraph_extra_falls_back_to_fsm(monkeypatch) -> None:
    """The Docker image syncs without extras, so this path must not raise."""
    monkeypatch.setattr(deps.settings, "engine", "langgraph")
    monkeypatch.setitem(sys.modules, "app.orchestration.graph", None)
    sentinel = object()
    assert deps._select_orchestrator(sentinel) is sentinel


def test_langgraph_engine_builds_a_dispatcher(monkeypatch) -> None:
    from app.orchestration.dispatch import EngineDispatcher

    monkeypatch.setattr(deps.settings, "engine", "langgraph")
    selected = deps._select_orchestrator(deps.fsm_pipeline)
    assert selected.describe() == {"engine": "langgraph"}
    assert isinstance(selected, EngineDispatcher)
    # The FSM pipeline stays reachable for the four fallback paths.
    assert selected._fsm is deps.fsm_pipeline


@pytest.mark.asyncio
async def test_healthz_reports_the_active_engine(monkeypatch) -> None:
    import app.routes.health as health_module

    monkeypatch.setattr(health_module, "_healthz_cache", {"at": 0.0, "result": None})
    result = await health_module.healthz()

    assert result["engine"] in ("fsm", "langgraph")
    assert "engine" not in result["checks"], "engine must not affect all_healthy"
