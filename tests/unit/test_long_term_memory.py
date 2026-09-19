from __future__ import annotations

import pytest

from app.long_term_memory import (
    LongTermMemory,
    extract_memory_ops,
    normalize_key,
)
from app.vectordb import VectorDBClient


def test_normalize_key_maps_aliases() -> None:
    assert normalize_key("名字") == "name"
    assert normalize_key("姓名") == "name"
    assert normalize_key(" 截 止 日 期 ") == "deadline"
    # 未收录标签按字面量归一，保证同一说法稳定落到同一槽位。
    assert normalize_key("  工单 编号 ") == "工单编号"


def test_extract_remember_chinese() -> None:
    ops = extract_memory_ops("记住我的名字是小张")
    assert len(ops) == 1
    op = ops[0]
    assert op.action == "remember"
    assert op.key == "name"
    assert op.label == "名字"
    assert op.value == "小张"


def test_extract_remember_with_prefix_and_punctuation() -> None:
    ops = extract_memory_ops("请记住项目截止日期是 10 月 1 日。")
    assert len(ops) == 1
    assert ops[0].key == "deadline"
    assert ops[0].value == "10 月 1 日"


def test_extract_remember_english() -> None:
    ops = extract_memory_ops("remember my name is Alice")
    assert len(ops) == 1
    assert ops[0].key == "name"
    assert ops[0].value == "Alice"


def test_extract_forget_and_forget_all() -> None:
    forget = extract_memory_ops("忘掉我的名字")
    assert len(forget) == 1
    assert forget[0].action == "forget"
    assert forget[0].key == "name"

    everything = extract_memory_ops("忘记我的所有记忆")
    assert len(everything) == 1
    assert everything[0].action == "forget_all"


def test_extract_ignores_plain_chatter() -> None:
    assert extract_memory_ops("今天天气不错") == []
    assert extract_memory_ops("") == []


@pytest.mark.asyncio
async def test_remember_then_recall_across_sessions() -> None:
    ltm = LongTermMemory(VectorDBClient())
    await ltm.remember("u1", "name", "小张", label="名字", session_id="s1")

    # 换一个 session 提问，仍应召回 s1 写入的事实。
    context = await ltm.recall_context("我的名字是什么", "u1")
    assert "小张" in context


@pytest.mark.asyncio
async def test_conflicting_update_overwrites_slot() -> None:
    ltm = LongTermMemory(VectorDBClient())
    await ltm.apply("u1", "记住我的名字是小张")
    await ltm.apply("u1", "记住我的名字是老王")

    docs = await ltm.list_for("u1")
    assert len(docs) == 1  # 同一槽位是覆盖，不是追加
    assert "老王" in docs[0].content

    context = await ltm.recall_context("我的名字", "u1")
    assert "老王" in context
    assert "小张" not in context


@pytest.mark.asyncio
async def test_users_are_isolated() -> None:
    ltm = LongTermMemory(VectorDBClient())
    await ltm.apply("u1", "记住我的名字是小张")

    assert await ltm.recall_context("我的名字", "u2") == ""
    assert await ltm.list_for("u2") == []


@pytest.mark.asyncio
async def test_forget_removes_only_target_slot() -> None:
    ltm = LongTermMemory(VectorDBClient())
    await ltm.apply("u1", "记住我的名字是小张")
    await ltm.apply("u1", "记住我的邮箱是 a@b.com")

    applied = await ltm.apply("u1", "忘掉我的名字")
    assert applied == ["forgotten:name:1"]

    assert await ltm.recall_context("我的名字", "u1") == ""
    remaining = await ltm.list_for("u1")
    assert len(remaining) == 1
    assert "邮箱" in remaining[0].content


@pytest.mark.asyncio
async def test_forget_all_clears_user_only() -> None:
    ltm = LongTermMemory(VectorDBClient())
    await ltm.apply("u1", "记住我的名字是小张")
    await ltm.apply("u2", "记住我的名字是小李")

    applied = await ltm.apply("u1", "忘记我的所有记忆")
    assert applied == ["forgotten_all:1"]

    assert await ltm.list_for("u1") == []
    assert len(await ltm.list_for("u2")) == 1
