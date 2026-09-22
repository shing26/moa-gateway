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
    # 工具级失败计数（工具不存在或 handler 抛异常）。ReAct 把这些失败降级成
    # observation 让模型自愈——这是有意设计——但**必须留下计数**，否则上层
    # 无法区分"任务真的完成"与"所有工具都失败但优雅降级"。
    tool_errors: int = 0