from __future__ import annotations

# 只保留真正被使用的部分：`store.py`（`/healthz` 用它的 RedisConfig/RedisStateStore）
# 与 `memory_fallback.py`（store 的降级实现）。
# `lock.py`（IdempotencyLock / LuaLockFactory）与 `stack.py`（状态栈）在 2026-09-23 被删除：
# 它们从未接入请求路径，而 HITL 的幂等已改用挂起记录自身的原子认领（pop_hitl），
# 留着就是"看起来有能力"的死代码。多实例会话状态真要外部化时，应按当时的真实需求重新设计。
from app.redis_state.store import RedisConfig, RedisStateStore

__all__ = [
    "RedisConfig",
    "RedisStateStore",
]
