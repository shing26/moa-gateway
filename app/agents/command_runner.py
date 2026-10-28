"""跑一条固定命令，返回可有判定的结果。

这里是 agent 工具与 harness 共用的子进程入口，免得"怎么跑命令"在两处各长一个
样子。网络不是真隔离：本模块做到的是剥掉代理与凭据类环境变量，并让工具集里
没有联网或装包的能力——这是软约束，不是沙箱。
"""

from __future__ import annotations

import logging
# Fixed verification commands require a process boundary; task argv is never shell-expanded.
import subprocess  # nosec B404
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.agents.subprocess_env import isolated_env

logger = logging.getLogger("moa.agents.command_runner")

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
    env = isolated_env(workspace, base=base)
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
        # argv 由任务定义给出，shell=False，字符串不会交给 shell 解释。
        proc = subprocess.run(  # noqa: S603  # nosec B603
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
