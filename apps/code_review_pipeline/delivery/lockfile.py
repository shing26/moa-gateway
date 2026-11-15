"""Stale-launch lock for delivery runs.

2026-11-12 的两次事故：被放弃的 delivery 进程 22 小时后还活着、占着
连接，因为没有任何机制发现或阻止重复启动。``acquire`` 在
``<delivery_run_record_dir>/.locks/`` 下落一个 JSON 锁文件
（``{"pid", "started_at", "host"}``）：持有者进程还活着就拒绝新启动，
持有者已死（进程退出但锁没删）就抢走并报告被替换的旧持有者。

aliveness 检查可注入（``alive`` 参数），单测不需要真实 pid；真实实现
在 Windows 上用 kernel32 ``OpenProcess``（只查询，不杀进程），POSIX
上用 ``os.kill(pid, 0)``。无新依赖。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("moa.delivery.lockfile")

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _pid_alive_windows(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    # 只开查询句柄、立即关闭：这检测进程是否存在，不是注入或终止。
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程在但归别人所有：活着。
        return True
    return True


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


@dataclass(frozen=True)
class LockInfo:
    """Holder identity as recorded in (or parsed from) the lock file."""

    pid: int | None
    started_at: str
    host: str


class LockHandle:
    """A held lock; ``release`` deletes the file only if we still own it."""

    def __init__(self, path: Path, info: LockInfo) -> None:
        self.path = path
        self.info = info

    def release(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 文件没了或读不出来：没有"我们的锁"可删，保持安静。
            return
        if not isinstance(payload, dict):
            return
        pid = payload.get("pid")
        host = payload.get("host")
        if pid != self.info.pid or host != self.info.host:
            logger.info(
                "lock %s was taken over (pid=%s); leaving the new holder alone",
                self.path,
                pid,
            )
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("failed to remove lock %s: %s", self.path, exc)


class StaleLockReplaced(LockHandle):
    """acquire() 成功抢走了一个死持有者的锁。

    也算持有锁（文件里现在是我们）：因此继承 ``LockHandle`` 的
    ``release()``，CLI 的 ``finally: lock.release()`` 在抢锁路径上同样
    成立。``previous`` 是被替换的旧持有者，供 CLI 打一行警告。
    """

    def __init__(self, path: Path, info: LockInfo, previous: LockInfo) -> None:
        super().__init__(path, info)
        self.previous = previous


def read_holder(path: Path) -> LockInfo | None:
    """Parse the current holder of ``path``, or None if absent/corrupt."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    return LockInfo(
        pid=pid if isinstance(pid, int) else None,
        started_at=str(payload.get("started_at", "")),
        host=str(payload.get("host", "")),
    )


def _write_payload(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def acquire(
    path: Path,
    *,
    alive: Callable[[int], bool] = _pid_alive,
) -> LockHandle | StaleLockReplaced | None:
    """Take the delivery lock at ``path``.

    Returns:
      - ``LockHandle`` when no lock existed (fresh acquisition);
      - ``StaleLockReplaced`` when a dead holder's lock was stolen (the
        old holder's identity rides along for the CLI warning);
      - ``None`` when a live holder refuses us — call :func:`read_holder`
        for the identity to print.

    A corrupt or pid-less lock cannot prove its holder is alive and is
    treated as stale.
    """
    path = Path(path)
    existed = path.exists()
    current = read_holder(path)
    if current is not None and current.pid is not None and alive(current.pid):
        logger.warning(
            "delivery lock %s held by live pid=%s started_at=%s host=%s",
            path,
            current.pid,
            current.started_at,
            current.host,
        )
        return None
    info = LockInfo(
        pid=os.getpid(),
        started_at=datetime.now(UTC).isoformat(),
        host=socket.gethostname(),
    )
    _write_payload(
        path,
        {
            "pid": info.pid,
            "started_at": info.started_at,
            "host": info.host,
        },
    )
    if existed:
        # 旧锁（死持有者，或损坏到读不出 pid）被我们覆盖：把旧持有者带回
        # 去，让 CLI 能打一行警告。current 为 None 说明文件损坏。
        previous = current or LockInfo(pid=None, started_at="", host="")
        logger.warning(
            "replaced stale delivery lock %s (old pid=%s started_at=%s)",
            path,
            previous.pid,
            previous.started_at,
        )
        return StaleLockReplaced(path=path, info=info, previous=previous)
    return LockHandle(path=path, info=info)
