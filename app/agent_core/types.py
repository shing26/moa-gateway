from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

ReActAction = Literal["call_tool", "finish"]

#: 降级上报回调（ADR-018 决策 7）：task-LLM 兜底时说明原因，由调用方汇进结果。
#: 之所以用回调而不是让 LLM 自己记事，是因为 `TaskAgent` 的 `self._llm` 是**进程内
#: 单例**（模块导入时构造一次），挂在实例上的可变状态会被并发请求互相踩。
DegradeCallback = Callable[[str], None]


@dataclass
class ReActDecision:
    action: ReActAction
    tool_name: str = ""
    arguments: dict[str, object] = field(default_factory=dict)
    note: str = ""
    final_answer: str = ""
    # 非空表示这个决策不是模型给的，而是降级兜底（LLM 调用失败或输出不是合法 JSON）。
    # 空字符串 = 正常决策。带上它，"基础设施崩了"才不会被当成"模型决定收尾"。
    degraded_reason: str = ""


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
    # 工具调用**尝试数**（不是成功数）：一次 call_tool 决策就算一次，无论其后
    # 成功、handler 抛异常还是参数被拒。口径必须是尝试数，否则"全部失败"的判据
    # （tool_errors + tool_arg_rejections == tool_calls）不成立——此前它记的是成功数，
    # 于是真·全失败反而不触发刹车（ADR-018「过程发现」）。
    # 成功数 = tool_calls - tool_errors - tool_arg_rejections。
    tool_calls: int = 0
    # 工具级失败计数（工具不存在或 handler 抛异常）。ReAct 把这些失败降级成
    # observation 让模型自愈——这是有意设计——但**必须留下计数**，否则上层
    # 无法区分"任务真的完成"与"所有工具都失败但优雅降级"。
    tool_errors: int = 0
    # 参数被拒计数（未声明参数 / 不符合工具 schema），**不含**在 tool_errors 里。
    # 分开是刻意的：tool_errors 是"执行了但失败"（环境问题），这里是"根本没执行"
    # （模型填错参数，它自己能修）。合成一个数会让"模型在瞎猜参数"与"后端挂了"
    # 在指标上长得一样——那正是 ADR-018 决策 3 要分开的两件事。
    tool_arg_rejections: int = 0
    # 本子任务里 task-LLM 的降级原因（ADR-018 决策 7）。非空 = 这个结果是**兜底**
    # 而不是模型判断，不该当正常交付；上层据此转人工。
    degraded_reasons: list[str] = field(default_factory=list)