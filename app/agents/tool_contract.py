"""工具调用契约：参数校验、`session_id` 注入与统一分发（ADR-018）。

为什么单独一层
--------------
在校验这件事之前，工具调用有**两条**各自为政的路径，且都不校验：

* ``app/agent_core/react.py`` 的 ``ReActLoop``：``args = dict(decision.arguments)``
  之后直接 ``tool.handler(**args)``；
* ``app/agents/stubs.py`` 的 ``_execute_with_tools``：``json.loads(...)`` 之后
  同样直接 ``tool.handler(**arguments)``。

两条路径各自演化的结果是同一个概念两种行为：前者用 ``setdefault`` 注入
``session_id``（**模型自带的值因此会压过系统值**，而 ``add_note``/``get_notes``
正是靠这个键做会话隔离 → 可跨会话读写）；后者**根本不注入**（``add_note`` 直接
败在"缺少会话标识"），但模型参数原样落到 ``handler(**arguments)``，所以模型只要
自己带上 ``session_id`` 就同样越界。

所以契约放在这里、两条路径共用：**校验在前、注入在前、只在一个地方决定
"模型给的参数能不能进 handler"**。

三个刻意的取舍
--------------
1. **`session_id` 用覆盖式赋值，不是 `setdefault`**：系统值永远胜过模型值。
   只在工具**声明**了该参数时注入。
2. **未声明的参数直接拒**：不这样，``handler(**args)`` 会以 ``TypeError`` 收场，
   于是"模型填错参数"和"环境坏了"落进同一个计数器（``tool_errors``）。提前拦下
   才能把两者分开。
3. **本模块自身的故障不迁怒模型**：工具声明的 schema 若本身非法（这是我们的 bug），
   记 error 日志后**放行**（退回未校验的旧行为），而不是把模型的这次调用判为非法。
   方向是不对称的——模型的错要拦，我们的错不能伪装成模型的错。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

import jsonschema

from app.agents.tools import AgentTool

logger = logging.getLogger("moa.agents.tool_contract")

#: 由系统注入、模型不得自行指定的参数名。
SESSION_ID_PARAM = "session_id"

OutcomeKind = Literal["ok", "rejected", "failed"]


@dataclass(frozen=True)
class ToolCallOutcome:
    """一次工具调用的结果。``detail`` 的含义随 ``kind`` 变化：

    * ``ok``       —— handler 的返回值原文
    * ``rejected`` —— 参数被拒的原因（简短中文，会回填给模型让它自纠）
    * ``failed``   —— handler 抛出的异常文本
    """

    kind: OutcomeKind
    detail: str


def _missing_required(err: jsonschema.ValidationError) -> str:
    names = re.findall(r"'([^']+)'", err.message)
    return f"缺少必填参数 {'、'.join(names)}" if names else "缺少必填参数"


def _format_error(err: jsonschema.ValidationError) -> str:
    """把 jsonschema 的报错压成一句模型能照做的中文。

    原文形如 ``'query' is not of type 'string'`` / ``'x' is a required property``，
    回填给模型时既啰嗦又不点名字段。这里按 validator 分派成固定句式，让模型能
    直接定位到"哪个参数、该是什么"。
    """
    field = ".".join(str(p) for p in err.path) or "(根)"
    if err.validator == "required":
        return _missing_required(err)
    if err.validator == "type":
        return f"参数 {field} 类型应为 {err.validator_value}"
    if err.validator == "enum":
        return f"参数 {field} 取值必须是 {list(err.validator_value)} 之一"
    return f"参数 {field} 不合法（{err.validator}）"


def validate_tool_args(tool: AgentTool, args: dict[str, Any]) -> str | None:
    """校验 ``args`` 是否符合 ``tool.parameters``。

    返回 ``None`` 表示通过；返回一句中文表示被拒的原因。区分"未声明参数"与
    "schema 不符"，但都归为拒绝——两者都不是"执行失败"。
    """
    schema = tool.parameters or {}
    problems: list[str] = []

    # ① 未声明的参数。以 "properties" 键是否存在（而非是否非空）为判据：
    #    无参工具声明的是 "properties": {}，任何参数都该拒。
    if "properties" in schema:
        declared = schema.get("properties") or {}
        unknown = sorted(k for k in args if k not in declared)
        if unknown:
            problems.append(f"参数 {'、'.join(unknown)} 未在工具声明中")

    # ② JSON Schema 校验
    try:
        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(args), key=lambda e: list(e.path))
    except Exception as exc:  # noqa: BLE001 — 只可能是我们自己写错的 schema
        logger.error(
            "tool %s 的 parameters 不是可用 JSON Schema，本次跳过校验: %s", tool.name, exc
        )
        return None
    for err in errors:
        # additionalProperties 的报错与 ① 是同一件事，不重复报
        if err.validator == "additionalProperties":
            continue
        problems.append(_format_error(err))

    return "；".join(problems) if problems else None


async def call_tool(
    tool: AgentTool, arguments: dict[str, Any] | None, *, session_id: str = ""
) -> ToolCallOutcome:
    """按契约执行一次工具调用：注入 → 校验 → 执行。

    这是两条调用路径（``ReActLoop`` 与 ``stubs._execute_with_tools``）的唯一入口，
    保证"模型给的参数能不能进 handler"只在这里被决定一次。
    """
    args: dict[str, Any] = dict(arguments or {})

    # 系统值覆盖模型值：模型自带的 session_id 一律作废（ADR-018 决策 2）。
    declared = (tool.parameters or {}).get("properties") or {}
    if SESSION_ID_PARAM in declared:
        args[SESSION_ID_PARAM] = session_id

    rejection = validate_tool_args(tool, args)
    if rejection is not None:
        logger.info(
            "tool %s 参数被拒 session=%s: %s", tool.name, session_id, rejection
        )
        return ToolCallOutcome("rejected", rejection)

    try:
        output = await tool.handler(**args)
    except Exception as exc:  # noqa: BLE001 — 工具失败降级成 observation 是有意设计
        return ToolCallOutcome("failed", str(exc))
    return ToolCallOutcome("ok", str(output))


__all__ = [
    "SESSION_ID_PARAM",
    "OutcomeKind",
    "ToolCallOutcome",
    "call_tool",
    "validate_tool_args",
]
