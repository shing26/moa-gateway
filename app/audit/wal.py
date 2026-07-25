from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.audit.models import AuditEntry

logger = logging.getLogger("moa.audit.wal")


@dataclass
class LogConfig:
    directory: str = "logs"
    retention_days: int = 90
    file_prefix: str = "audit"


@dataclass
class AsyncWal:
    _buffer: deque[AuditEntry] = field(default_factory=lambda: deque(maxlen=100_000))
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _max_bytes: int = 1 * 1024 * 1024 * 1024
    _config: LogConfig = field(default_factory=LogConfig)

    def _log_path(self, dt: date | None = None) -> str:
        if dt is None:
            dt = date.today()
        d = Path(self._config.directory)
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"{self._config.file_prefix}-{dt.isoformat()}.jsonl")

    def _cleanup_old_logs(self) -> None:
        cutoff = date.today() - timedelta(days=self._config.retention_days)
        d = Path(self._config.directory)
        if not d.exists():
            return
        deleted = 0
        for f in d.iterdir():
            if f.name.startswith(self._config.file_prefix) and f.name.endswith(".jsonl"):
                try:
                    file_date_str = f.name[len(self._config.file_prefix) + 1:-6]
                    file_date = date.fromisoformat(file_date_str)
                    if file_date < cutoff:
                        f.unlink()
                        deleted += 1
                except (ValueError, OSError):
                    continue
        if deleted:
            logger.info("wal cleaned %d old log files", deleted)

    async def append(self, entry: AuditEntry) -> None:
        with self._lock:
            if self.estimated_bytes >= self._max_bytes:
                logger.warning("wal byte limit reached, dropping oldest entry")
                self._buffer.popleft()
            self._buffer.append(entry)
            path = self._log_path()
            self._write_disk(entry, path)
        logger.debug("wal append trace=%s", entry.trace_id)

    async def replay(self, batch_size: int = 100) -> list[AuditEntry]:
        with self._lock:
            batch = []
            while self._buffer and len(batch) < batch_size:
                batch.append(self._buffer.popleft())
            return batch

    async def replay_all(self) -> list[AuditEntry]:
        with self._lock:
            entries = list(self._buffer)
            self._buffer.clear()
            return entries

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._buffer)

    @property
    def estimated_bytes(self) -> int:
        with self._lock:
            return sum(len(e.agent_output) for e in self._buffer)

    def _write_disk(self, entry: AuditEntry, path: str) -> None:
        try:
            line = json.dumps({
                "trace_id": entry.trace_id,
                "session_id": entry.session_id,
                "agent_name": entry.agent_name,
                "intent": entry.intent,
                "timestamp": entry.timestamp.isoformat(),
                "agent_output_len": len(entry.agent_output),
                "eval_score": entry.eval_score,
                "eval_issues": list(entry.eval_issues),
                "guard_action": entry.guard_action,
                "guard_reason": entry.guard_reason,
            }, ensure_ascii=False)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            logger.error("wal disk write error: %s", exc)
        self._cleanup_old_logs()

    def close(self) -> None:
        pass


__all__ = ["AsyncWal", "AuditEntry", "LogConfig"]
