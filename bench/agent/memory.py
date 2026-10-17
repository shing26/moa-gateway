"""有界 in-context 轨迹。

§5.0 要求 agent"知道自己已经试过什么，不重复撞同一面墙"。最省事也最危险的做法是
把所有观察都留着：长任务会把上下文撑爆，而**溢出是以 ``error`` 形式出现的**——
按 §8，``error`` 是要单独剔除的噪音，于是"上下文没管好"会被统计成"API 抖动"。
所以轨迹有界：最近若干轮完整保留，更早的压成一行摘要。

复用 ``app/context_budget.py`` 的 ``estimate_tokens`` / ``fit_text``（形状中立），
但**不套** ``compact_history``——那个函数压的是"对话历史 + 摘要"，形状是
``list[{role, content}]``，而轨迹里还有 ``tool_calls`` 与 ``tool_call_id`` 的配对
关系，硬套会把配对拆散。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.context_budget import fit_text


@dataclass(frozen=True)
class MemoryPolicy:
    #: 完整保留的最近轮数。一轮 = 一条 assistant 消息及其后续的 tool 消息。
    max_full_turns: int = 6
    #: 单条观察回给模型时的 token 上限。
    max_observation_tokens: int = 2_000


def truncate_observation(text: str, policy: MemoryPolicy) -> str:
    """按预算截断一条观察。观察是成本的主要来源，所以这一步是硬性的。"""
    clipped, _ = fit_text(text or "", policy.max_observation_tokens)
    return clipped


def compact_messages(
    messages: list[dict[str, Any]], policy: MemoryPolicy
) -> list[dict[str, Any]]:
    """保留最近 ``max_full_turns`` 轮，更早的压成一行摘要。"""
    if policy.max_full_turns <= 0:
        return [dict(item) for item in messages]

    head: list[dict[str, Any]] = []
    turns: list[list[dict[str, Any]]] = []
    for message in messages:
        if message.get("role") == "assistant":
            turns.append([dict(message)])
        elif turns:
            turns[-1].append(dict(message))
        else:
            head.append(dict(message))

    if len(turns) <= policy.max_full_turns:
        return head + [item for turn in turns for item in turn]

    cut = len(turns) - policy.max_full_turns
    dropped, kept = turns[:cut], turns[cut:]
    lines = [
        f"第 {index + 1} 轮：{summarize_turn(turn)}"
        for index, turn in enumerate(dropped)
    ]
    note = {
        "role": "system",
        "content": f"（早期 {len(dropped)} 轮行动已省略，摘要如下）\n" + "\n".join(lines),
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


__all__ = ["MemoryPolicy", "compact_messages", "summarize_turn", "truncate_observation"]
