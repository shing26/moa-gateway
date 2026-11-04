"""有界 in-context 轨迹：保留窗口、压缩摘要、观察截断。

这条策略原先只存在于 ``bench/agent``。交付主链路（``app/agents/stubs.py`` 的
``execute_tool_loop``）没有它，于是真实交付里出现了一个可复核的失败形状：
17 次调用累计 101 万 prompt token，39 步全是 read/search、零写入——上下文被
历史观察撑满，预算在"还没开始改"之前就烧完了。

**可复核性与上下文预算不冲突**：Run Record 仍然保留每一步的完整观察（审计证据），
压缩只作用于重新发给模型的消息列表。两者是两份数据，不是一个东西。

复用 ``app/context_budget.py`` 的 ``estimate_tokens`` / ``fit_text``（形状中立），
但**不套** ``compact_history``——那个函数压的是"对话历史 + 摘要"，形状是
``list[{role, content}]``，而轨迹里还有 ``tool_calls`` 与 ``tool_call_id`` 的配对
关系，硬套会把配对拆散。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.context_budget import estimate_tokens, fit_text

_SUMMARY_HEADER = re.compile(r"^（早期 (\d+) 轮行动已省略，摘要如下）\n?")


@dataclass(frozen=True)
class MemoryPolicy:
    #: 完整保留的最近轮数。一轮 = 一条 assistant 消息及其后续的 tool 消息。
    max_full_turns: int = 6
    #: 单条观察回给模型时的 token 上限。
    max_observation_tokens: int = 2_000


def _fit_head(text: str, budget: int) -> str:
    """保留头部，最多 ``budget`` token（不附加截断标记）。"""
    if estimate_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low]


def _fit_tail(text: str, budget: int) -> str:
    """保留尾部，最多 ``budget`` token（不附加截断标记）。"""
    if estimate_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[-mid:]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[len(text) - low :]


def truncate_observation(
    text: str, policy: MemoryPolicy, *, keep_tail: bool = False
) -> str:
    """按预算截断一条观察。观察是成本的主要来源，所以这一步是硬性的。

    ``keep_tail=True`` 用于命令输出：pytest 的失败摘要、退出码附近的信息都在
    尾部，只留头部会把最该看的部分裁掉。文件读取则相反，头部通常已包含结构与
    签名，默认保留头部即可。
    """
    text = text or ""
    if not keep_tail:
        clipped, _ = fit_text(text, policy.max_observation_tokens)
        return clipped
    if policy.max_observation_tokens <= 0 or not text:
        return text
    if estimate_tokens(text) <= policy.max_observation_tokens:
        return text
    # 尾部是命令结论所在：约三分之一给头部（命令回显），三分之二给尾部（失败摘要）。
    head_budget = max(1, policy.max_observation_tokens // 3)
    tail_budget = max(1, policy.max_observation_tokens - head_budget)
    head = _fit_head(text, head_budget)
    tail = _fit_tail(text, tail_budget)
    omitted = max(0, len(text) - len(head) - len(tail))
    return f"{head}\n…（中间省略 {omitted} 字符）…\n{tail}"


def compact_messages(
    messages: list[dict[str, Any]], policy: MemoryPolicy
) -> list[dict[str, Any]]:
    """保留最近 ``max_full_turns`` 轮，更早的压成**一份**摘要。

    上一轮的摘要不能被当成普通头部消息原样留下——否则每次压缩都新增一条
    "早期 1 轮"，40 步之后会有几十条摘要。这里把既有摘要解析出来，与新丢弃的
    轮次合并成一条（编号累计）。
    """
    if policy.max_full_turns <= 0:
        return [dict(item) for item in messages]

    head: list[dict[str, Any]] = []
    prior_summaries: list[dict[str, Any]] = []
    turns: list[list[dict[str, Any]]] = []
    for message in messages:
        if message.get("role") == "assistant":
            turns.append([dict(message)])
        elif turns:
            turns[-1].append(dict(message))
        elif message.get("role") == "system" and _SUMMARY_HEADER.match(
            str(message.get("content", ""))
        ):
            prior_summaries.append(dict(message))
        else:
            head.append(dict(message))

    if len(turns) <= policy.max_full_turns:
        return (
            head
            + prior_summaries
            + [item for turn in turns for item in turn]
        )

    prior_count = 0
    prior_lines: list[str] = []
    for note in prior_summaries:
        content = str(note.get("content", ""))
        match = _SUMMARY_HEADER.match(content)
        if match:
            prior_count += int(match.group(1))
            prior_lines.extend(content[match.end():].splitlines())

    cut = len(turns) - policy.max_full_turns
    dropped, kept = turns[:cut], turns[cut:]
    new_lines = [f"- {summarize_turn(turn)}" for turn in dropped]
    note = {
        "role": "system",
        "content": (
            f"（早期 {prior_count + len(dropped)} 轮行动已省略，摘要如下）\n"
            + "\n".join(prior_lines + new_lines)
        ),
    }
    return head + [note] + [item for turn in kept for item in turn]


def summarize_turn(turn: list[dict[str, Any]]) -> str:
    """把一轮压成一行：调用了什么工具 → 看到了什么。"""
    assistant = turn[0] if turn else {}
    calls = assistant.get("tool_calls") or []
    if not calls:
        return "结束"
    names = ", ".join(
        str(call.get("function", {}).get("name", "?")) for call in calls
    )
    for message in turn[1:]:
        if message.get("role") == "tool":
            snippet = str(message.get("content", "")).strip().replace("\n", " ")
            return f"调用 {names} → {snippet[:80]}"
    return f"调用 {names}"


__all__ = [
    "MemoryPolicy",
    "compact_messages",
    "estimate_tokens",
    "summarize_turn",
    "truncate_observation",
]
