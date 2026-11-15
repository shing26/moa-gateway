"""Run heartbeat: an asyncio liveness stamp for a delivery run.

2026-11-12 复盘的观测：run record 只在状态跳转时落盘，而 litellm 的
keep-alive 连接让"跑了 10 分钟"和"死了 10 分钟"从外面看一模一样——
那天运维据此误杀了 7+ 个健康 run。heartbeat 以固定间隔把 UTC 时间戳
原子写到 ``<record_dir>/<run_id>.heartbeat``，run 退出时删除；时间戳
不再刷新就是可见的死亡证据。

约束（delivery-watchdog-v1 契约）：只有 asyncio，没有线程；任何写入
失败都只记日志，绝不把异常抛进交付流程。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("moa.delivery.heartbeat")

DEFAULT_INTERVAL_S = 30.0
INTERVAL_ENV_VAR = "DELIVERY_HEARTBEAT_S"


def _default_interval_s() -> float:
    raw = os.environ.get(INTERVAL_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "invalid %s=%r; falling back to %ss",
            INTERVAL_ENV_VAR,
            raw,
            DEFAULT_INTERVAL_S,
        )
        return DEFAULT_INTERVAL_S
    if value <= 0:
        logger.warning(
            "%s must be positive, got %s; falling back to %ss",
            INTERVAL_ENV_VAR,
            value,
            DEFAULT_INTERVAL_S,
        )
        return DEFAULT_INTERVAL_S
    return value


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


class Heartbeat:
    """A single asyncio task writing a UTC timestamp to ``path`` on an interval.

    Start with :meth:`start` (it needs a running event loop); stop with
    :meth:`stop`, which cancels the task and deletes the stamp file.  A
    failed write is logged and never raised — a broken heartbeat must not
    abort a delivery run.
    """

    def __init__(self, path: Path, interval_s: float) -> None:
        self._path = Path(path)
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    @classmethod
    def start(cls, path: Path, interval_s: float | None = None) -> "Heartbeat":
        """Stamp immediately, then rewrite every ``interval_s`` seconds.

        ``interval_s=None`` reads ``DELIVERY_HEARTBEAT_S`` (fallback 30).
        """
        if interval_s is None:
            interval_s = _default_interval_s()
        heartbeat = cls(path, interval_s)
        heartbeat._write_once()
        heartbeat._task = asyncio.get_running_loop().create_task(heartbeat._run())
        return heartbeat

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            self._write_once()

    def _write_once(self) -> None:
        try:
            _write_atomic(self._path, datetime.now(UTC).isoformat() + "\n")
        except Exception as exc:
            logger.warning(
                "heartbeat write failed for %s: %s", self._path, exc
            )

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def stop(self) -> None:
        """Cancel the writer task and delete the stamp file.  Never raises."""
        if self._stopped:
            return
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "heartbeat cleanup failed for %s: %s", self._path, exc
            )


def read_age_s(path: Path, *, now: datetime | None = None) -> float | None:
    """Seconds since the heartbeat was last written, or ``None``.

    ``None`` covers both a missing file and a corrupt/unparseable stamp —
    both mean "cannot prove liveness".  Pass ``now`` for deterministic
    tests.
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    reference = now if now is not None else datetime.now(UTC)
    return (reference - stamp).total_seconds()
