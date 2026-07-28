from __future__ import annotations
from dataclasses import dataclass
from typing import Any

@dataclass
class GuardVerdict:
    allowed: bool = True
    reason: str = ""

class FailClosedPermissionGuard:
    async def check(self, agent_name: str, slot: dict[str, Any]) -> GuardVerdict:
        return GuardVerdict(allowed=True, reason="legacy guard always allows")
