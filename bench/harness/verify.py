"""跑一条固定命令，返回可有判定的结果。

这里是 bench 里唯一的子进程入口：``bench/agent/tools.py`` 的 ``run_tests`` 与
harness 的验证都走它，免得"怎么跑命令"在两处各长一个样子。

网络不是真隔离
--------------
§7.4 要求"不给网络"。本模块做到的是**剥掉代理与凭据类环境变量**，并让工具集里
没有联网或装包的能力——这是软约束，不是沙箱。真要硬隔离得进容器，那与本次
"先把原语做对"的范围不成比例。这里如实记下，不假装是沙箱。
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("bench.harness.verify")

#: 剥掉的环境变量：代理与凭据。它们既能带出网络，也能让"环境里恰好有 token"
#: 变成一次和编排无关的差异来源。
_STRIPPED_SUFFIXES = ("_PROXY", "_API_KEY")
_STRIPPED_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "OMNIROUTE_API_KEY",
    }
)

#: 防止被验证的命令往工作区里写缓存，把 patch 弄脏。
_DEFAULT_PYTEST_ADDOPTS = "-p no:cacheprovider"


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def build_env(workspace: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """构造子进程环境：剥代理/凭据，把 workspace 放到 ``PYTHONPATH`` 最前。"""
    env = dict(os.environ if base is None else base)
    for name in list(env):
        upper = name.upper()
        if upper in _STRIPPED_NAMES or upper.endswith(_STRIPPED_SUFFIXES):
            env.pop(name, None)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{workspace}{os.pathsep}{existing}" if existing else str(workspace)
    )
    # 别在工作区里留 __pycache__/.pytest_cache：patch 是判决的证据，不该混进噪音。
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTEST_ADDOPTS", _DEFAULT_PYTEST_ADDOPTS)
    return env


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float = 300.0,
    output_limit: int = 20000,
) -> CommandResult:
    """跑一条命令。**超时记为非零退出**，不抛异常——按 §8，超时是结果不是崩溃。

    唯一的例外是命令本身起不来（可执行文件不存在、权限不够）：那抛
    ``OSError``，由调用方决定怎么记账。
    """
    try:
        proc = subprocess.run(  # noqa: S603 - argv 由任务定义给出，不经 shell
            list(argv),
            cwd=str(cwd),
            env=build_env(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = _as_text(exc.stdout) + _as_text(exc.stderr)
        return CommandResult(
            exit_code=124,
            output=_clip(f"[验证命令超时 {timeout_s:.0f}s]\n{output}", output_limit),
            timed_out=True,
        )
    output = (proc.stdout or "") + (proc.stderr or "")
    return CommandResult(
        exit_code=proc.returncode, output=_clip(output, output_limit)
    )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _clip(text: str, limit: int) -> str:
    """保留头尾两段。验证失败的关键信息通常在尾部（pytest 的失败摘要）。"""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return f"{text[:head]}\n…（中略 {len(text) - limit} 字符）…\n{text[-tail:]}"


__all__ = ["CommandResult", "build_env", "run_command"]
