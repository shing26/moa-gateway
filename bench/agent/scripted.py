"""离线确定性决策器：与在线 LLM 同接口，供 CI 与确定性测试用。

沿用 ``MockTaskLLM`` / ``ScriptedExpert`` 的先例：真实现是循环、工具、记账、
停止条件，只有"下一步做什么"由脚本给出。这样 CI 能在零网络、零 token 下把
整条路径跑完，包括**失败路径**——而失败路径正是 §5.0 要测的东西。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agents.provider import ChatResult


@dataclass(frozen=True)
class _Config:
    """与 ``LLMClient.config`` 同形，让循环能读到 model / temperature。"""

    model: str
    temperature: float


@dataclass(frozen=True)
class ScriptedTurn:
    """一轮的动作。``tool`` 非空即调用工具，否则视为终局。"""

    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    final: str = ""
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 非空表示这一轮"调用失败"——用来测 ``status="error"`` 那条路径。
    error: str = ""


class ScriptedLLM:
    """按脚本逐轮吐决策，并报告用量。"""

    def __init__(
        self,
        turns: Sequence[ScriptedTurn],
        *,
        model: str = "scripted",
        temperature: float = 0.0,
    ) -> None:
        self._turns = list(turns)
        self._cursor = 0
        self.config = _Config(model=model, temperature=temperature)
        self.last_metrics: dict[str, Any] = {}

    def _next_turn(self) -> ScriptedTurn:
        if self._cursor >= len(self._turns):
            # 脚本用完就当作"没有更多动作"——确定性终局，而不是死循环。
            return ScriptedTurn(final="")
        turn = self._turns[self._cursor]
        self._cursor += 1
        return turn

    async def chat_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs: Any
    ) -> ChatResult:
        turn = self._next_turn()
        if turn.error:
            raise RuntimeError(turn.error)
        self.last_metrics = {
            "model_used": self.config.model,
            "cost_usd": turn.cost_usd,
            "llm_latency_ms": 0.0,
            "prompt_tokens": turn.prompt_tokens,
            "completion_tokens": turn.completion_tokens,
        }
        if turn.tool:
            call = {
                "id": f"scripted-call-{self._cursor}",
                "type": "function",
                "function": {
                    "name": turn.tool,
                    "arguments": json.dumps(turn.arguments, ensure_ascii=False),
                },
            }
            return ChatResult(
                messages=[*messages, {"role": "assistant", "content": "", "tool_calls": [call]}],
                content="",
                tool_calls=[call],
            )
        return ChatResult(
            messages=[*messages, {"role": "assistant", "content": turn.final}],
            content=turn.final,
            tool_calls=[],
        )


__all__ = ["ScriptedLLM", "ScriptedTurn"]
