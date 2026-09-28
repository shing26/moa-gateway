from __future__ import annotations

import hashlib
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

#: 哈希不能覆盖它自己。
_HASH_FIELDS = ("prev_hash", "entry_hash")
#: 恢复链头时向后读的字节数——只需要最后一行，不必读整个文件。
_RESUME_TAIL_BYTES = 64 * 1024


def _canonical_payload(record: dict[str, Any]) -> str:
    """把一条审计记录压成确定性字符串，**只用于算哈希**。

    键排序 + 紧凑分隔符：同一份内容在任何机器、任何字典序下算出的哈希必须一致。
    排掉哈希字段本身，否则会自指。
    """
    body = {k: v for k, v in record.items() if k not in _HASH_FIELDS}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _chain_hash(prev_hash: str, payload: str) -> str:
    return hashlib.sha256(f"{prev_hash}{payload}".encode()).hexdigest()


def verify_chain(path: str | Path) -> int | None:
    """校验一个 WAL 文件的哈希链：返回**第一个**断裂处的行号（1-based），完整则 None。

    逐行校验三件事，任一不成立即断裂：① 本行 `prev_hash` 等于上一行的 `entry_hash`；
    ② `entry_hash` 等于按同一规则重算的值；③ 首行的 `prev_hash` 为空。

    两处**刻意**的边界，不是遗漏：

    * **每个日文件是一条独立的链**，跨日不接续，所以首行的 `prev_hash` 是空的。
      进程重启会从当日文件尾恢复链头，因此同一天内是连续的。
    * 恢复失败（文件损坏 / 读不动）或遇到**史前条目**（本功能之前的旧行没有哈希字段）时，
      校验会**如实报出**断裂。这是对的：能证明的是"从这条起可证连续"，
      "无法证明连续"应当被看见，而不是被抹平。

    文件不存在时返回 None（没有链可校验）。
    """
    p = Path(path)
    if not p.exists():
        return None
    prev_hash = ""
    with p.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                return lineno
            if not isinstance(record, dict) or record.get("prev_hash", "") != prev_hash:
                return lineno
            expected = _chain_hash(prev_hash, _canonical_payload(record))
            if record.get("entry_hash", "") != expected:
                return lineno
            prev_hash = expected
    return None


def _default_log_dir() -> str:
    from app.config import settings

    return settings.log_dir


def _default_retention_days() -> int:
    from app.config import settings

    return settings.log_retention_days


@dataclass
class LogConfig:
    # 目录与保留期跟 settings（LOG_DIR / LOG_RETENTION_DAYS）走。此前这里写死
    # "logs"/90，.env.template 文档里的这两个旋钮**没有任何代码读**——是死配置，
    # 2026-09-24 收编进 app/config.py 时一并复活（测试据此把日志隔离到临时目录）。
    directory: str = field(default_factory=_default_log_dir)
    retention_days: int = field(default_factory=_default_retention_days)
    file_prefix: str = "audit"


@dataclass
class AsyncWal:
    _buffer: deque[AuditEntry] = field(default_factory=lambda: deque(maxlen=100_000))
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _max_bytes: int = 1 * 1024 * 1024 * 1024
    _config: LogConfig = field(default_factory=LogConfig)
    # 缓冲里每条 entry 对应的**实际写入行字节数**。容量判据用它而不是
    # `len(agent_output)`——此前的口径既不等于写入量（落盘时 agent_output 被 pop 掉），
    # 也不是字节（len(str) 是字符数，中文差 3 倍）。
    _sizes: deque[int] = field(default_factory=deque)
    # 链头：上一条已写入记录的 entry_hash。None = 还没从磁盘恢复过（懒加载）。
    _last_hash: str | None = None
    # 缓冲满溢被挤掉的条数。此前 `deque(maxlen=…)` 是**静默**丢最旧，没有任何计数——
    # 审计说"丢了多少"必须答得出来（ADR-019 决策 4）。
    _dropped: int = 0
    # 落盘失败次数。链头写失败时审计**继续**（进程内接链），但降级必须可见。
    _write_failures: int = 0

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
            path = self._log_path()
            if self._last_hash is None:
                self._last_hash = self._resume_chain(path)
            line, entry_hash = self._render_line(entry, self._last_hash)
            incoming = len(line.encode())
            self._evict_if_needed(incoming)
            self._buffer.append(entry)
            self._sizes.append(incoming)
            entry.prev_hash = self._last_hash
            entry.entry_hash = entry_hash
            # 先推进链头再落盘：写失败时审计仍在**进程内**继续接链（ADR-019 决策 2），
            # 而不是让一次 IO 抖动把整条审计链路掐死。
            self._last_hash = entry_hash
            self._write_line(line, path)
        logger.debug("wal append trace=%s", entry.trace_id)

    def _render_line(self, entry: AuditEntry, prev_hash: str) -> tuple[str, str]:
        """生成落盘行与它的 ``entry_hash``。

        哈希覆盖**实际写下的字段**（``to_audit_dict()`` 去掉 agent_output 全文）。
        若让哈希覆盖被 pop 掉的字段，从日志文件本身就不再能重算校验——那等于
        链只在内存里成立，落盘之后就没法验证了。
        """
        record = entry.to_audit_dict()
        record.pop("agent_output", None)
        record["prev_hash"] = prev_hash
        record["entry_hash"] = ""
        record["entry_hash"] = _chain_hash(prev_hash, _canonical_payload(record))
        return json.dumps(record, ensure_ascii=False), record["entry_hash"]

    def _evict_if_needed(self, incoming_bytes: int) -> None:
        """给即将到来的条目腾位。腾位必须**可见**（计数 + 告警），不许静默丢。

        此前 ``deque(maxlen=100_000)`` 满时自己丢最旧，没有任何痕迹；字节上限那条
        分支也只打了一条 warning、没有计数，且判据用的是错的容量口径。
        """
        maxlen = self._buffer.maxlen
        if maxlen is not None and len(self._buffer) >= maxlen:
            self._dropped += 1
            self._sizes.popleft()
            logger.warning(
                "wal buffer full (%d), evicting oldest entry (dropped=%d)",
                maxlen, self._dropped,
            )
            return
        projected = self.buffered_bytes + incoming_bytes
        if self._buffer and projected > self._max_bytes:
            self._dropped += 1
            self._sizes.popleft()
            self._buffer.popleft()
            logger.warning(
                "wal byte budget exceeded (%d > %d), evicting oldest entry (dropped=%d)",
                projected, self._max_bytes, self._dropped,
            )

    def _resume_chain(self, path: str) -> str:
        """从当日文件尾恢复链头，使进程重启后链仍连续。

        失败时返回空串——新条目因此以空 ``prev_hash`` 起链，``verify_chain`` 会**如实**
        把它报成断裂。"无法证明与之前连续"应当可见，而不是悄悄补一个看似连续的链头。
        """
        p = Path(path)
        if not p.exists():
            return ""
        try:
            with p.open("rb") as f:
                size = p.stat().st_size
                if size > _RESUME_TAIL_BYTES:
                    f.seek(size - _RESUME_TAIL_BYTES)
                tail = f.read().decode("utf-8", errors="replace")
            last = next(ln for ln in reversed(tail.splitlines()) if ln.strip())
            record = json.loads(last)
            return str(record.get("entry_hash", "") or "")
        except (OSError, ValueError, StopIteration, json.JSONDecodeError) as exc:
            logger.warning("wal chain resume failed, starting a new chain: %s", exc)
            return ""

    async def replay(self, batch_size: int = 100) -> list[AuditEntry]:
        with self._lock:
            batch = []
            while self._buffer and len(batch) < batch_size:
                batch.append(self._buffer.popleft())
                self._sizes.popleft()
            return batch

    async def replay_all(self) -> list[AuditEntry]:
        with self._lock:
            entries = list(self._buffer)
            self._buffer.clear()
            self._sizes.clear()
            return entries

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._buffer)

    @property
    def buffered_bytes(self) -> int:
        """缓冲内各条**实际写入行**的字节数之和。

        此前这个属性叫 ``estimated_bytes`` 且求的是 ``len(agent_output)``——既不是
        写入量（落盘时该字段被 pop 掉），也不是字节（``len(str)`` 是字符数）。
        名字与口径一起改，免得下一个人再按错的语义用它。
        """
        with self._lock:
            return sum(self._sizes)

    @property
    def dropped(self) -> int:
        """缓冲满溢被挤掉的条数。>0 说明审计窗口丢过东西（不再是静默的）。"""
        with self._lock:
            return self._dropped

    @property
    def write_failures(self) -> int:
        """落盘失败次数。>0 表示磁盘上的链可能有洞，但进程内仍在接链。"""
        with self._lock:
            return self._write_failures

    def _write_line(self, line: str, path: str) -> None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            # 落盘失败不中断审计：链头已在内存里推进，后续条目继续接链。
            self._write_failures += 1
            logger.error(
                "wal disk write error (failures=%d): %s", self._write_failures, exc
            )
        self._cleanup_old_logs()

    def close(self) -> None:
        pass


__all__ = ["AsyncWal", "AuditEntry", "LogConfig", "verify_chain"]
