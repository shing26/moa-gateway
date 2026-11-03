"""兼容入口：有界上下文策略已提升到 ``app/agents/memory``。

交付主链路与实验台现在共用同一份实现——压缩策略是运行时契约，不是实验台
私有物。保留本模块只是为了让既有 import 与测试不必同时改道。
"""

from app.agents.memory import (
    MemoryPolicy,
    compact_messages,
    summarize_turn,
    truncate_observation,
)

__all__ = ["MemoryPolicy", "compact_messages", "summarize_turn", "truncate_observation"]
