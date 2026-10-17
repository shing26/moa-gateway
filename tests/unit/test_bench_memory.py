"""§5.0 的有界 in-context 轨迹：保留窗口、压缩摘要、观察截断。"""

from __future__ import annotations

from typing import Any

from bench.agent.memory import (
    MemoryPolicy,
    compact_messages,
    summarize_turn,
    truncate_observation,
)


def _turn(index: int, note: str) -> list[dict[str, Any]]:
    """一轮 = 一条带 tool_calls 的 assistant 消息 + 它对应的 tool 消息。"""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"call-{index}",
            "name": "read_file",
            "content": note,
        },
    ]


def test_recent_turns_are_kept_verbatim() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
    ]
    for index in range(2):
        messages += _turn(index, f"观察 {index}")
    assert compact_messages(messages, MemoryPolicy(max_full_turns=2)) == messages


def test_older_turns_collapse_into_one_summary() -> None:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
    for index in range(3):
        messages += _turn(index, f"观察 {index}")

    out = compact_messages(messages, MemoryPolicy(max_full_turns=1))

    # 头部保留 → 一条摘要 → 最近一轮（assistant + tool 两条）原样保留。
    assert out[0] == {"role": "system", "content": "sys"}
    summary = out[1]
    assert summary["role"] == "system"
    assert "早期 2 轮" in summary["content"]
    assert "调用 read_file" in summary["content"]
    assert [item["role"] for item in out[2:]] == ["assistant", "tool"]
    assert out[-1]["content"] == "观察 2"


def test_non_positive_window_disables_compaction() -> None:
    messages: list[dict[str, Any]] = [{"role": "assistant", "content": "x"}]
    assert compact_messages(messages, MemoryPolicy(max_full_turns=0)) == messages


def test_summarize_turn_marks_a_terminal_turn() -> None:
    assert summarize_turn([{"role": "assistant", "content": "done"}]) == "结束"


def test_truncate_observation_honours_the_token_budget() -> None:
    long_text = "字" * 500
    out = truncate_observation(long_text, MemoryPolicy(max_observation_tokens=20))
    assert out != long_text
    assert "超出预算已截断" in out
    assert len(out) < len(long_text)
