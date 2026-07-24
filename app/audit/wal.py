from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.audit.models import AuditEntry

logger = logging.getLogger("moa.audit.wal")


@dataclass
class AsyncWal:
    """Local async write-ahead log with optional disk persistence.

    - In-memory deque buffer (estimated max 1 GB raw text).
    - Optional JSONL file on disk for crash recovery.
    - Provides :meth:eplay for backfill when downstream recovers.
    """

    _buffer: deque[AuditEntry] = field(default_factory=lambda: deque(maxlen=100_000))
    _disk_path: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _max_bytes: int = 1 * 1024 * 1024 * 1024  # 1 GB

    def set_disk_path(self, path: str) -> None:
        self._disk_path = path

    async def append(self, entry: AuditEntry) -> None:
        with self._lock:
            if self.estimated_bytes >= self._max_bytes:
                logger.warning("wal byte limit reached, dropping oldest entry")
                self._buffer.popleft()
            self._buffer.append(entry)
            if self._disk_path:
                self._write_disk(entry)
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

    def _write_disk(self, entry: AuditEntry) -> None:
        try:
            line = json.dumps({
                "trace_id": entry.trace_id,
                "session_id": entry.session_id,
                "agent_name": entry.agent_name,
                "intent": entry.intent,
                "timestamp": entry.timestamp.isoformat(),
                "agent_output_len": len(entry.agent_output),
            }, ensure_ascii=False)
            with open(self._disk_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            logger.error("wal disk write error: %s", exc)

    def close(self) -> None:
        pass


__all__ = ["AsyncWal", "AuditEntry"]
