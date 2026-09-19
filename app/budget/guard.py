"""预算 guard（M6）：per-session 成本累计与限额拦截。

语义（成本只能在 LLM 调用后得知，所以现实可行的模式是
"调用后累计 + 超限后拒后续"，无法在单次调用前精确拦截）：

- ``limit_usd <= 0``：只核算不拦截（默认），与未引入本层时行为零差异；
- ``limit_usd > 0``：会话累计成本达到限额后，该会话后续请求在 agent
  执行前被拒——FSM 侧状态 ``blocked``/``error_code=budget_exceeded``，
  LangGraph 侧短路到 ``blocked`` 节点，审计 ``guard_action=budget_exceeded``。

累计器在进程内（与 Redis 不可用时的内存回退同级的已知边界）：
单实例语义正确，多实例部署需要把累计器外部化（Redis INCR 等），
那是后续工作，不与 ``app/redis_state`` 现状绑定。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _SessionSpend:
    total_usd: float = 0.0
    last_seen: float = field(default_factory=time.monotonic)


class BudgetGuard:
    def __init__(self, limit_usd: float = 0.0, ttl_seconds: float = 86400.0) -> None:
        self.limit_usd = float(limit_usd)
        self.ttl_seconds = float(ttl_seconds)
        self._spend: dict[str, _SessionSpend] = {}

    @property
    def enforcing(self) -> bool:
        """False 表示只核算（默认），不改变任何请求路径的行为。"""
        return self.limit_usd > 0

    def check(self, session_id: str) -> bool:
        """该会话是否允许继续发起请求；非 enforcing 恒 True。"""
        if not self.enforcing:
            return True
        self._evict_expired()
        entry = self._spend.get(session_id)
        return entry is None or entry.total_usd < self.limit_usd

    def record(self, session_id: str, cost_usd: float) -> float:
        """累计一次真实成本，返回该会话的新累计值。"""
        if cost_usd <= 0:
            return self.spent(session_id)
        entry = self._spend.setdefault(session_id, _SessionSpend())
        entry.total_usd += float(cost_usd)
        entry.last_seen = time.monotonic()
        return entry.total_usd

    def spent(self, session_id: str) -> float:
        entry = self._spend.get(session_id)
        return entry.total_usd if entry else 0.0

    def reset(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._spend.clear()
        else:
            self._spend.pop(session_id, None)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, entry in self._spend.items()
            if now - entry.last_seen > self.ttl_seconds
        ]
        for sid in expired:
            self._spend.pop(sid, None)


__all__ = ["BudgetGuard"]
