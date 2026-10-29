from __future__ import annotations

import os
import sys
from pathlib import Path

from app.agents.command_runner import build_verify_env
from app.agents.subprocess_env import isolated_env


def test_verify_env_prepends_the_active_virtualenv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    venv = tmp_path / "venv"
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))

    env = build_verify_env(
        tmp_path / "workspace",
        base={"PATH": str(tmp_path / "host-bin")},
    )

    assert env["VIRTUAL_ENV"] == str(venv)
    path_parts = env["PATH"].split(os.pathsep)
    assert path_parts == [str(bin_dir), str(tmp_path / "host-bin")]


def test_isolated_env_keeps_its_allowlist_semantics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))

    env = isolated_env(tmp_path / "workspace", base={"PATH": "/usr/bin"})

    assert env["PATH"] == "/usr/bin"
    assert "VIRTUAL_ENV" not in env
