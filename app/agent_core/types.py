from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ReActAction = Literal["call_tool", "finish"]


@dataclass
class ReActDecision:
    action: ReActAction
    tool_name: str = ""
    arguments: dict[str, object] = field(default_factory=dict)
    note: str = ""
    final_answer: str = ""


@dataclass
class TaskStep:
    index: int
    decision: ReActDecision
    observation: str = ""


@dataclass
class TaskResult:
    answer: str
    steps: list[TaskStep]
    plan: list[str] = field(default_factory=list)
    tool_calls: int = 0