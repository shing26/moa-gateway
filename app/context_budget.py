"""上下文预算、裁剪与压缩（对应评测标准的"上下文工程"）。

背景：此前上下文是"有多少塞多少"——``memory.get_history`` 默认取最近 10 轮
（最多 20 条消息），长期记忆与检索结果直接拼接。本地小模型上下文只有
1024～4096 token，一旦溢出，模型行为直接退化（丢指令、串味、答非所问）。
本模块在把上下文交给模型之前做三件事：

1. **估算**：启发式 token 估算（CJK 1 字≈1 token，其余 4 字符≈1 token），
   保守偏大——宁可早裁剪，也不要真溢出。不引入 tokenizer 依赖，离线可测。
2. **裁剪**：历史从最新往回整轮保留，超预算的旧对话不再整段丢弃。
3. **压缩**：被裁掉的旧对话压成一条"省略摘要"（extractive：每轮取开头
   若干字符），模型仍知道聊过什么，只是细节退化。

优先级（越靠后越先被裁）：当前输入 > 系统提示 > 检索上下文 ≈ 长期记忆 >
省略摘要 > 更早的历史。系统提示与工具描述是固定开销，不在此预算内。

预算为 0 表示该项禁用（保持既有行为），便于按模型能力配置：
本机 1024 上下文的演示模型应把两个预算都调到 384 左右。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 每轮消息的角色/分隔开销（保守常数，随内容一起计入预算）
_MESSAGE_OVERHEAD_TOKENS = 8
# 省略摘要中每条旧消息保留的字符数
_ELISION_SNIPPET_CHARS = 40
# 截断标记（其自身也占预算）
_TRUNCATION_MARKER = "\n…（上下文超出预算已截断）"
_TRUNCATION_MARKER_TOKENS = 14  # 见 tests：estimate_tokens(_TRUNCATION_MARKER)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF      # CJK 统一表意
        or 0x3000 <= code <= 0x303F   # CJK 标点
        or 0xFF00 <= code <= 0xFFEF   # 全角
        or 0x3040 <= code <= 0x30FF   # 假名
    )


def estimate_tokens(text: str) -> int:
    """保守的 token 估算：CJK 按字计，其余按 4 字符 1 token。"""
    if not text:
        return 0
    cjk = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
    others = len(text) - cjk
    return cjk + (others + 3) // 4


def fit_text(text: str, budget_tokens: int) -> tuple[str, bool]:
    """按预算截断文本（保留头部，截掉尾部）。budget<=0 表示禁用。"""
    if budget_tokens <= 0 or not text:
        return text, False
    if estimate_tokens(text) <= budget_tokens:
        return text, False
    # 截断标记自身也占预算：先扣掉，保证返回值真的在预算内
    target = max(1, budget_tokens - estimate_tokens(_TRUNCATION_MARKER))
    ratio = target / max(1, estimate_tokens(text))
    cut = max(1, int(len(text) * ratio))
    while cut > 0 and estimate_tokens(text[:cut]) > target:
        cut = int(cut * 0.9) if cut > 10 else cut - 1
    return text[:cut] + _TRUNCATION_MARKER, True


@dataclass
class HistoryBudgetResult:
    history: list[dict[str, Any]]
    elision: str
    kept_messages: int
    dropped_messages: int
    kept_tokens: int
    elided: bool


def compact_history(
    history: list[dict[str, Any]],
    budget_tokens: int,
) -> HistoryBudgetResult:
    """按预算从最新往回保留历史，被裁掉的旧消息压成一条省略摘要。

    ``budget_tokens <= 0`` 表示禁用预算：原样返回，行为与引入本模块前一致。
    摘要本身按剩余预算截断；连摘要都放不下时干脆不产出摘要。
    """
    if budget_tokens <= 0 or not history:
        return HistoryBudgetResult(
            history=list(history or []),
            elision="",
            kept_messages=len(history or []),
            dropped_messages=0,
            kept_tokens=sum(estimate_tokens(str(m.get("content", ""))) for m in (history or [])),
            elided=False,
        )

    kept: list[dict[str, Any]] = []
    used = 0
    for msg in reversed(history):
        cost = estimate_tokens(str(msg.get("content", ""))) + _MESSAGE_OVERHEAD_TOKENS
        if used + cost > budget_tokens:
            break
        kept.append(msg)
        used += cost
    kept.reverse()

    # 裁剪后若以非 user 消息开头，说明它对应的提问已被裁掉：孤儿回复会让模型
    # 误读上下文，直接去掉。
    while kept and kept[0].get("role") != "user":
        kept = kept[1:]

    # 退化保底：若一条完整轮次都放不下（或清完孤儿后为空），保留最新一条——
    # 能装下就原样保留，装不下则截断。丢掉"用户刚说了什么"比留下一条截断的
    # 上下文更糟。
    if not kept:
        newest = dict(history[-1])
        content = str(newest.get("content", ""))
        room = budget_tokens - _MESSAGE_OVERHEAD_TOKENS
        if estimate_tokens(content) > room:
            if room > _TRUNCATION_MARKER_TOKENS:
                content, _ = fit_text(content, room)
            else:
                # 连截断标记都放不下：宁可不带内容，也不超预算
                content = ""
            newest["content"] = content
        kept = [newest]
        used = estimate_tokens(str(newest["content"])) + _MESSAGE_OVERHEAD_TOKENS

    dropped = history[: len(history) - len(kept)]
    elision = ""
    if dropped:
        snippets = []
        for msg in dropped:
            content = str(msg.get("content", "")).strip().replace("\n", " ")
            snippet = content[:_ELISION_SNIPPET_CHARS]
            snippets.append(f"{msg.get('role', '?')}: {snippet}")
        elision = f"（早期 {len(dropped)} 条对话已省略，摘要如下）\n" + "\n".join(snippets)
        # 摘要占用历史预算的剩余额度；放不下就不要摘要
        remaining = budget_tokens - used
        if remaining <= 0:
            elision = ""
        else:
            elision, _ = fit_text(elision, remaining)

    return HistoryBudgetResult(
        history=kept,
        elision=elision,
        kept_messages=len(kept),
        dropped_messages=len(dropped),
        kept_tokens=used,
        elided=bool(elision),
    )


@dataclass(frozen=True)
class ContextBudget:
    """一次部署的上下文预算策略：组合根构造，双引擎共用同一实例。

    0 表示该项禁用（保持既有行为）。**预算只覆盖"历史 + 摘要"两块**：
    系统提示与工具描述是固定开销、不计入，因此本策略不承诺"总上下文绝不
    溢出"，只保证随会话增长的两块不会无限膨胀。1024 窗口的模型建议两项都设
    384（见 README 已知边界）。
    """

    history_tokens: int = 0
    summary_tokens: int = 0

    @property
    def enabled(self) -> bool:
        return self.history_tokens > 0 or self.summary_tokens > 0


@dataclass(frozen=True)
class ContextBudgetResult:
    history: list[dict[str, Any]]
    summary: str
    stats: dict[str, Any]


def apply_context_budget(
    history: list[dict[str, Any]] | None,
    summary: str,
    budget: "ContextBudget | None",
) -> ContextBudgetResult:
    """双引擎唯一的上下文预算入口：历史裁剪 → 省略摘要合并 → 摘要截断。

    两条引擎（MoAPipeline / LangGraphOrchestrator）都只调用本函数，避免各自
    实现导致送进 agent 的上下文漂移（ADR-008 的等价性要求）。budget 为
    None 或未启用时原样返回，行为与引入本模块前完全一致。
    """
    history = list(history or [])
    summary = summary or ""
    if budget is None or not budget.enabled:
        return ContextBudgetResult(
            history=history,
            summary=summary,
            stats={"enabled": False},
        )

    compaction = compact_history(history, budget.history_tokens)
    merged = summary
    if compaction.elision:
        merged = f"{merged}\n\n---\n\n{compaction.elision}" if merged else compaction.elision
    merged, truncated = fit_text(merged, budget.summary_tokens)
    return ContextBudgetResult(
        history=compaction.history,
        summary=merged,
        stats={
            "enabled": True,
            "history_in": len(history),
            "history_kept": compaction.kept_messages,
            "history_dropped": compaction.dropped_messages,
            "elided": compaction.elided,
            "summary_tokens": estimate_tokens(merged),
            "summary_truncated": truncated,
            "budget": {"history": budget.history_tokens, "summary": budget.summary_tokens},
        },
    )


__all__ = [
    "ContextBudget",
    "ContextBudgetResult",
    "HistoryBudgetResult",
    "apply_context_budget",
    "compact_history",
    "estimate_tokens",
    "fit_text",
]
