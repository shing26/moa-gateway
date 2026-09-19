from __future__ import annotations

# 意图标签（路由器输出）→ Agent 注册键 的同构对齐表。
#
# 修复根因 A：路由器输出 coding/translate/search/... 等语义标签，
# 而 AGENT_REGISTRY 仅注册 coder/general/review 三个键。原 pipeline.py
# 直接用意图标签去查注册表，导致 CoderAgent / ReviewAgent 永不被选中。
# 通过本表将"意图标签集"与"注册键集"对齐，未知意图一律回退 general。
INTENT_AGENT_MAP: dict[str, str] = {
    "coding": "coder",
    "translate": "general",
    "summarize": "general",
    "search": "general",
    "analyze": "general",
    "greeting": "general",
    "debug": "general",
    "control": "general",
    "assistant": "general",
    "default": "general",
    "review": "review",
    "task": "task",
}

# 注册表实际存在的键（与 app/agents/loader.py 注册一致）。
_AGENT_KEYS = frozenset({"coder", "general", "review", "task"})


def resolve_agent_key(intent: str) -> str:
    """将意图标签解析为注册键；未知意图回退 general。"""
    return INTENT_AGENT_MAP.get(intent, "general")


# 启动期自检：映射值必须是合法注册键，否则立即失败，避免再次出现根因 A 的键不匹配。
_invalid = {k: v for k, v in INTENT_AGENT_MAP.items() if v not in _AGENT_KEYS}
if _invalid:
    raise RuntimeError(f"INTENT_AGENT_MAP 含未知注册键: {_invalid}")
