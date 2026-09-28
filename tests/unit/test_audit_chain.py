"""审计链与容量口径测试（ADR-019）。

背景：此前 README 声称"**不可篡改**审计"，而实现是明文 append、`deque(maxlen=…)`
满时静默丢最旧、容量算的是 `len(agent_output)`（既不是写入量也不是字节）。
本文件钉住修正后的三件事：

1. **能发现**篡改——链式哈希做到的是 tamper-evident，不是 tamper-proof；
2. **丢了多少条答得出来**——满溢有计数与告警，不再静默；
3. **容量算的是真写入字节**（中文用例把"字节"与"字符"分开）。

边界也一并钉住：每个日文件是独立的链（首行 prev_hash 为空）、史前条目（无哈希字段）
会被如实报成断裂、落盘失败不中断进程内接链。
"""

from __future__ import annotations

import json
from collections import deque

import pytest

from app.audit.models import AuditEntry
from app.audit.wal import AsyncWal, LogConfig, verify_chain


def _wal(tmp_path, **kwargs) -> AsyncWal:
    return AsyncWal(
        _config=LogConfig(directory=str(tmp_path), retention_days=90), **kwargs
    )


def _entry(i: int) -> AuditEntry:
    return AuditEntry(
        trace_id=f"t{i}", session_id="s1", agent_name="coder",
        agent_output=f"out{i}", intent="coding", eval_score=1.0,
    )


async def _append_n(wal: AsyncWal, n: int) -> None:
    for i in range(n):
        await wal.append(_entry(i))


def _log_file(tmp_path):
    return next(iter(tmp_path.glob("audit-*.jsonl")))


def _rows(tmp_path) -> list[dict]:
    text = _log_file(tmp_path).read_text(encoding="utf-8")
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def _rewrite(tmp_path, rows: list[dict]) -> None:
    _log_file(tmp_path).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


# ── 链本身 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chain_links_consecutive_entries(tmp_path) -> None:
    wal = _wal(tmp_path)
    await _append_n(wal, 3)

    rows = _rows(tmp_path)
    assert rows[0]["prev_hash"] == "", "当日首行是一条独立链的头"
    for i in (1, 2):
        assert rows[i]["prev_hash"] == rows[i - 1]["entry_hash"]
    assert verify_chain(_log_file(tmp_path)) is None


@pytest.mark.asyncio
async def test_chain_survives_a_process_restart(tmp_path) -> None:
    """新实例（= 进程重启）要从当日文件尾接上链头，否则"连续"一重启就断。"""
    first = _wal(tmp_path)
    await _append_n(first, 2)
    last_hash = _rows(tmp_path)[-1]["entry_hash"]

    second = _wal(tmp_path)
    await second.append(_entry(99))

    rows = _rows(tmp_path)
    assert rows[2]["prev_hash"] == last_hash
    assert verify_chain(_log_file(tmp_path)) is None


# ── 篡改检测 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_detects_a_tampered_field(tmp_path) -> None:
    wal = _wal(tmp_path)
    await _append_n(wal, 3)

    rows = _rows(tmp_path)
    rows[1]["intent"] = "被改过"
    _rewrite(tmp_path, rows)

    assert verify_chain(_log_file(tmp_path)) == 2, "改过的那一行必须被点名"


@pytest.mark.asyncio
async def test_verify_detects_a_tampered_prev_hash(tmp_path) -> None:
    wal = _wal(tmp_path)
    await _append_n(wal, 3)

    rows = _rows(tmp_path)
    rows[2]["prev_hash"] = "0" * 64
    _rewrite(tmp_path, rows)

    assert verify_chain(_log_file(tmp_path)) == 3


@pytest.mark.asyncio
async def test_verify_detects_a_removed_row(tmp_path) -> None:
    """抽掉中间一行，后续行的 prev_hash 就对不上了。"""
    wal = _wal(tmp_path)
    await _append_n(wal, 4)

    rows = _rows(tmp_path)
    del rows[1]
    _rewrite(tmp_path, rows)

    assert verify_chain(_log_file(tmp_path)) == 2


def test_verify_flags_rows_written_before_the_chain_existed(tmp_path) -> None:
    """史前条目没有哈希字段——如实报断裂，而不是当它"链完整"。"""
    f = tmp_path / "audit-2020-01-01.jsonl"
    f.write_text(json.dumps({"trace_id": "old", "intent": "coding"}) + "\n", encoding="utf-8")

    assert verify_chain(f) == 1


def test_verify_missing_file_has_nothing_to_verify(tmp_path) -> None:
    assert verify_chain(tmp_path / "nope.jsonl") is None


# ── 容量与满溢（不许静默）──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buffer_overflow_is_counted(tmp_path) -> None:
    """满溢要留下计数——此前 deque(maxlen=…) 自己丢最旧，没有任何痕迹。"""
    wal = _wal(tmp_path, _buffer=deque(maxlen=2))
    await _append_n(wal, 3)

    assert wal.size == 2
    assert wal.dropped == 1


@pytest.mark.asyncio
async def test_no_overflow_means_no_dropped(tmp_path) -> None:
    wal = _wal(tmp_path, _buffer=deque(maxlen=8))
    await _append_n(wal, 3)

    assert wal.dropped == 0


@pytest.mark.asyncio
async def test_byte_budget_eviction_is_counted(tmp_path) -> None:
    """字节上限那条分支同样要计数（此前只 warning、且判据用错了口径）。"""
    wal = _wal(tmp_path)
    wal._max_bytes = 10  # 小到第二条就超
    await _append_n(wal, 3)

    assert wal.dropped >= 1


@pytest.mark.asyncio
async def test_replay_keeps_size_accounting_in_sync(tmp_path) -> None:
    """replay 会从缓冲里取走条目，字节账本必须跟着走，否则容量判据会失真。"""
    wal = _wal(tmp_path)
    await _append_n(wal, 3)
    assert wal.buffered_bytes > 0

    await wal.replay(batch_size=2)
    assert wal.size == 1
    assert wal.buffered_bytes > 0

    await wal.replay_all()
    assert wal.buffered_bytes == 0


# ── 落盘失败不得中断审计（ADR-019 决策 2）──────────────────────────────────


@pytest.mark.asyncio
async def test_write_failure_does_not_break_the_in_process_chain(tmp_path, monkeypatch) -> None:
    """链头写失败时审计**继续**：进程内接链 + 计数，而不是整条链路死掉。

    磁盘上因此可能有个洞——但"降级"是可见的（`write_failures` 计数 + error 日志），
    不是静默的。
    """
    import app.audit.wal as wal_module

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(wal_module, "open", _boom, raising=False)

    wal = _wal(tmp_path)
    first, second = _entry(0), _entry(1)
    await wal.append(first)
    await wal.append(second)

    assert wal.write_failures == 2
    assert first.entry_hash and second.entry_hash
    assert second.prev_hash == first.entry_hash, "写失败之后链仍要在进程内接上"
