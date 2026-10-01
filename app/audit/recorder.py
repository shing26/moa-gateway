"""审计写入的唯一出口（D4）。

**为什么必须单点**：``AsyncWal`` 维护一条**跨行的哈希链**（ADR-019：每条的
 ``prev_hash`` 是上一条的 ``entry_hash``）。若 worker 进程自己 new 一个
 ``AsyncWal``，它就会与网关进程的 WAL **各写各的链**，落到同一个 jsonl 文件里——
 于是 ``verify_chain`` 看到的是两条交错的链，整天的审计都被判成断裂。
所以 WAL 实例必须唯一，这里是它的家；``request_logger`` 也从这里取。

**为什么需要它**：审计此前只由 HTTP 中间件写，而 D3 之后 PR 审查跑在 worker
 进程里、**不经过 HTTP**。若不补这个出口，5 个 agent 的执行痕迹就完全落在
  审计之外——任务在队列里发生了什么，事后查不到。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.audit.models import AuditEntry
from app.audit.wal import AsyncWal

logger = logging.getLogger("moa.audit.recorder")

# 全进程唯一的 WAL。请求路径与 worker 路径共用它，链才不会分叉。
wal = AsyncWal()


async def record(entry: AuditEntry) -> None:
    """写一条审计到 WAL 与 ES（两个 sink 都失败也不抛——审计不该拖垮主流程）。"""
    await wal.append(entry)
    try:
        from app.deps import es_writer

        if es_writer is not None:
            await es_writer.write(entry)
    except Exception:
        logger.warning("es audit write failed", exc_info=True)


def entries_for_trace(logs_dir: str | Path, trace_id: str) -> list[dict[str, Any]]:
    """按 trace_id 从审计文件里捞出全部条目（跨文件、按写入顺序）。

    D4 的验收判据就是"按 task_id 能捞出 7 条且链完整"——这个函数是那条判据的
    读取端。跨文件是因为 WAL 按天分文件，而一次任务完全可能跨过零点。

    读不出来 / 行坏了就**跳过**而不是抛：审计文件被手工编辑过是运维事故，
    查询工具不该因此整个挂掉（链校验才是发现这类事故的地方）。
    """
    out: list[dict[str, Any]] = []
    directory = Path(logs_dir)
    if not directory.exists():
        return out
    for path in sorted(directory.glob("audit-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("trace_id") == trace_id:
                out.append(row)
    return out
