from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from app.agents.command_runner import build_verify_env, resolve_program, run_command
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


def test_resolve_program_replaces_a_bare_name_with_the_path_hit(
    tmp_path: Path,
) -> None:
    """裸名交给 CreateProcess/execvp 会按父进程 PATH 解析，不看我们的 env。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = bin_dir / ("python.exe" if os.name == "nt" else "python")
    target.write_text("", encoding="utf-8")
    target.chmod(0o755)

    resolved = resolve_program(["python", "-m", "pytest"], {"PATH": str(bin_dir)})

    # shutil.which 在大写 PATHEXT 命中时返回 python.EXE，比较时忽略大小写。
    assert os.path.normcase(resolved[0]) == os.path.normcase(str(target))
    assert resolved[1:] == ["-m", "pytest"]


def test_resolve_program_leaves_explicit_or_unknown_programs_alone() -> None:
    assert resolve_program(
        [sys.executable, "-c", "pass"], {"PATH": ""}
    ) == [sys.executable, "-c", "pass"]
    assert resolve_program(
        ["definitely-not-a-real-program-moa"], {"PATH": ""}
    ) == ["definitely-not-a-real-program-moa"]


def test_run_command_starts_the_interpreter_that_verify_env_puts_first(
    tmp_path: Path,
) -> None:
    """回归：验证副本里没有 .venv，裸写 python 必须仍落到管线的解释器。"""
    result = run_command(
        ("python", "-c", "import sys; print(sys.executable)"),
        cwd=tmp_path,
    )

    assert result.exit_code == 0, result.output
    expected = shutil.which("python", path=build_verify_env(tmp_path)["PATH"])
    assert expected is not None
    assert result.output.strip().lower() == expected.lower()
