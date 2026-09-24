"""审计落盘字段覆盖守卫（2026-09-24）。

背景：README 已声称 `route_fallback` / `tool_calls` / `tool_errors` /
`context_budget` 等字段"写入审计、可按 trace 查询"，但 `app/audit/wal.py`
与 `app/audit/es_writer.py` 各自手写了一份字段白名单，新字段在落盘时被
**静默丢弃**——只留在内存 ``AuditEntry.extra`` 里。后果已经可见：
``app/services/audit_stats.py`` 在读 ``violation`` / ``hitl_duration_ms``
两个从未落盘的字段（恒取默认值），ES 侧还比 WAL 少若干字段，两个 sink
的审计数据互相不一致。

所以这里不是"再补几个字段"，而是"让这类字段丢不掉"：字段集合只由
``AuditEntry.to_audit_dict()`` 提供，本文件守住三件事——

1. 每个 ``AuditEntry`` 字段都出现在序列化输出里（新增 dataclass 字段忘了写进
   出口 → 红）；
2. 每个由 ``request_logger`` 产出的 extra 键都真的落到盘上（README 的
   "可按 trace 查询"因此成立）；
3. 两个 sink 都从同一出口取字段，不再各自手写清单。

守卫自身也被测（喂一个被丢掉的字段，断言检查会红）——否则它只是个不会响的
警报（项目方法论⑨）。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from app.audit.es_writer import EsConfig, EsWriter
from app.audit.models import AuditEntry
from app.audit.wal import AsyncWal, LogConfig
from app.middleware import request_logger
from app.middleware.request_logger import bind_context_stats, log_request

REPO_ROOT = Path(__file__).resolve().parents[2]

# dataclass 上的类型化字段（extra 是自由字典，单独校验）
TYPED_FIELDS = {f.name for f in dataclasses.fields(AuditEntry)} - {"extra"}

# WAL 日志行的唯一表示差异：不落 agent_output 全文（只留 agent_output_len）以控制体积
WAL_OMITTED = {"agent_output"}

# request_logger 能产出的全部 extra 键。改这个文件时若新增了键，
# 下面的 test_extra_keys_are_all_serialized 会通过 to_audit_dict 自动覆盖；
# 若有人把 extra 从序列化出口里摘掉，它会红。
REQUEST_LOGGER_EXTRA_KEYS = {
    "method",
    "path",
    "status",
    "duration_ms",
    "input_preview",
    "output_preview",
    "policy_hits",
    "hitl_decision",
    "hitl_duration_ms",
    "llm_model",
    "cost_usd",
    "llm_latency_ms",
    "fallback_used",
    "context_budget",
    "retry_count",
    "retry_reason",
    "hitl_kind",
    "tool_calls",
    "tool_errors",
    "route_fallback",
    "hitl_operator",
}


def _missing_typed_fields(record: dict) -> set[str]:
    """序列化输出里缺了哪些类型化字段。抽成纯函数以便自测守卫本身。"""
    return TYPED_FIELDS - set(record)


def _full_entry() -> AuditEntry:
    """一条把所有字段都填满的审计条目（含每个 extra 键）。"""
    return AuditEntry(
        trace_id="t-full",
        session_id="s-full",
        agent_name="general",
        agent_output="模型输出",
        intent="search",
        eval_score=0.3,
        eval_issues=("contains_unfinished_marker",),
        guard_action="review",
        guard_reason="价格承诺",
        policy_hits=("no-price-commitment",),
        violation="no-price-commitment",
        hitl_decision="approve",
        hitl_duration_ms=42.0,
        extra={key: f"<{key}>" for key in REQUEST_LOGGER_EXTRA_KEYS},
    )


# ── 出口本身 ────────────────────────────────────────────────────────────────


def test_every_typed_field_is_serialized():
    """新增 AuditEntry 字段却忘了写进 to_audit_dict → 红。"""
    record = _full_entry().to_audit_dict()
    assert _missing_typed_fields(record) == set()


def test_extra_keys_are_all_serialized():
    """extra 是平铺进输出的：审计 JSONL 的既定约定是扁平键。"""
    record = _full_entry().to_audit_dict()
    assert REQUEST_LOGGER_EXTRA_KEYS <= set(record)


def test_typed_fields_win_over_same_named_extra_keys():
    """同名键以 dataclass 上的值为准，保证确定性。"""
    entry = _full_entry()
    entry.extra["guard_reason"] = "被 extra 覆盖的假值"
    assert entry.to_audit_dict()["guard_reason"] == "价格承诺"


# ── 两个 sink 真的落盘 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wal_persists_every_field(monkeypatch, tmp_path):
    """README 声称可按 trace 查询的字段，必须真的在日志行里。"""
    wal = AsyncWal(_config=LogConfig(directory=str(tmp_path), retention_days=90))
    monkeypatch.setattr(request_logger, "_wal", wal)
    bind_context_stats({"kept": 3, "dropped": 1, "elided": 1, "tokens": 512})

    class FakeRequest:
        method = "POST"
        url = "http://test/webhook/feishu"

    await log_request(
        FakeRequest(), 200, 12.3, "s1", "general", "search", "review",
        "输入", "输出",
        policy_hits=("no-price-commitment",),
        hitl_decision="approve",
        hitl_duration_ms=42.0,
        llm_model="qwen2.5:0.5b",
        cost_usd=0.001,
        llm_latency_ms=123.4,
        fallback_used="fallback-model",
        eval_score=0.3,
        eval_issues=("contains_unfinished_marker",),
        retry_count=1,
        retry_reason="llm_timeout",
        hitl_kind="review",
        tool_calls=3,
        tool_errors=3,
        route_fallback="none",
        hitl_operator="ou_approver",
    )

    files = list(tmp_path.glob("audit-*.jsonl"))
    assert files, "WAL 没有落盘"
    record = json.loads(files[0].read_text(encoding="utf-8").strip().splitlines()[-1])

    # ① 类型化字段（除 WAL 显式省略的全文）
    missing_typed = _missing_typed_fields(record) - WAL_OMITTED
    assert missing_typed == set(), f"WAL 丢了类型化字段: {missing_typed}"
    assert record["agent_output_len"] == len("输出")

    # ② request_logger 产出的 extra 键——README 的"可按 trace 查询"指的就是这些
    missing_extra = REQUEST_LOGGER_EXTRA_KEYS - set(record)
    assert missing_extra == set(), f"WAL 丢了 extra 字段: {missing_extra}"

    # ③ 抽查取值（不是只断言键存在）
    assert record["route_fallback"] == "none"
    assert record["context_budget"]["tokens"] == 512
    assert record["tool_calls"] == 3 and record["tool_errors"] == 3
    assert record["hitl_operator"] == "ou_approver"
    assert record["violation"] == "no-price-commitment"
    assert record["hitl_duration_ms"] == 42.0


def test_es_doc_covers_every_field():
    """ES 侧与 WAL 同源，此前它比 WAL 还少若干字段。"""
    writer = EsWriter(config=EsConfig(hosts=["http://localhost:9200"]))
    body = writer._build_bulk_body([_full_entry()]).decode("utf-8")
    doc = json.loads(body.splitlines()[1])

    assert _missing_typed_fields(doc) == set()
    assert REQUEST_LOGGER_EXTRA_KEYS <= set(doc)
    # ES 约定字段名，且保留全文以便检索
    assert doc["@timestamp"] == doc["timestamp"]
    assert doc["agent_output"] == "模型输出"


def test_wal_and_es_agree_on_field_set():
    """两个 sink 的字段集合必须一致（WAL 只省略全文那一个键）。"""
    entry = _full_entry()
    writer = EsWriter(config=EsConfig(hosts=["http://localhost:9200"]))
    es_doc = json.loads(writer._build_bulk_body([entry]).decode("utf-8").splitlines()[1])

    wal_keys = set(entry.to_audit_dict()) - WAL_OMITTED
    es_keys = set(es_doc) - {"@timestamp"}
    assert wal_keys <= es_keys
    # ES 比 WAL 多的，只允许是那一个全文键
    assert es_keys - wal_keys == WAL_OMITTED


# ── 守卫自身要会红（方法论⑨）───────────────────────────────────────────────


def test_coverage_guard_detects_a_dropped_field():
    """喂一个被丢掉的字段，断言检查会红——否则守卫只是装饰。"""
    record = _full_entry().to_audit_dict()
    pruned = {k: v for k, v in record.items() if k != "guard_reason"}
    assert _missing_typed_fields(pruned) == {"guard_reason"}


def test_writers_do_not_hand_maintain_field_lists():
    """两个 writer 必须从 to_audit_dict 取字段，不得再各写一份白名单。

    静态检查，防止有人"为了显式"把字段清单抄回 writer——那正是丢字段的成因。
    """
    for name in ("wal.py", "es_writer.py"):
        source = (REPO_ROOT / "app" / "audit" / name).read_text(encoding="utf-8")
        assert "to_audit_dict" in source, f"{name} 没有从单一出口取字段"
