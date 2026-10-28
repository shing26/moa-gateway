"""harness 的子进程入口：剥环境、把 workspace 顶到 PYTHONPATH 最前、超时记为结果。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from bench.harness.verify import build_env, run_command


def test_build_env_uses_an_explicit_allowlist(tmp_path: Path) -> None:
    env = build_env(
        tmp_path,
        {
            "PATH": "/usr/bin",
            "TEMP": "/tmp",
            "HTTP_PROXY": "http://proxy",
            "HTTPS_PROXY": "http://proxy",
            "OPENAI_API_KEY": "sk-secret",
            "OMNIROUTE_API_KEY": "secret",
            "LLM_API_KEY": "secret",
            "SOME_SERVICE_API_KEY": "secret",
            "GITHUB_TOKEN": "token",
            "DATABASE_URL": "postgresql://u:p@db/app",
            "VECTOR_DB_DSN": "postgresql://u:p@db/vectors",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "UNRELATED_FLAG": "must-not-be-inherited",
        },
    )
    for stripped in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "OPENAI_API_KEY",
        "OMNIROUTE_API_KEY",
        "LLM_API_KEY",
        "SOME_SERVICE_API_KEY",
        "GITHUB_TOKEN",
        "DATABASE_URL",
        "VECTOR_DB_DSN",
        "AWS_SECRET_ACCESS_KEY",
        "UNRELATED_FLAG",
    ):
        assert stripped not in env
    assert env["PATH"] == "/usr/bin"
    assert env["TEMP"] == "/tmp"


def test_build_env_puts_workspace_first_on_pythonpath(tmp_path: Path) -> None:
    env = build_env(tmp_path, {"PYTHONPATH": "/existing"})
    assert env["PYTHONPATH"].split(os.pathsep) == [str(tmp_path), "/existing"]
    # 别在工作区里留 __pycache__，否则它会混进作为判决证据的 patch。
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_run_command_reports_exit_code_and_output(tmp_path: Path) -> None:
    result = run_command([sys.executable, "-c", "print('hi')"], cwd=tmp_path)
    assert result.passed is True
    assert result.exit_code == 0
    assert "hi" in result.output


def test_timeout_is_a_result_not_a_crash(tmp_path: Path) -> None:
    """§8：超时是结果（记 fail），不是把 harness 自己搞崩。"""
    result = run_command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout_s=0.5,
    )
    assert result.timed_out is True
    assert result.passed is False
    assert result.exit_code == 124
