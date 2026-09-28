from __future__ import annotations

import asyncio
import ast
import logging
import operator
from datetime import datetime
from typing import Any

from app.agents.tools import AgentTool, tool_registry

logger = logging.getLogger("moa.agent_core.tools_extra")

# ── 安全计算器（仅允许数字、算术运算符、括号）────────────────────────────

_ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

# 上界（ADR-018 决策 5）。此前 calculator 没有任何界：`9**9**9` 会让 _eval_node
# 在事件循环里同步算出天文数字级的整数，把整个进程卡住——它既是 DoS，又是
# "async 函数里跑纯 CPU" 的典型坏味道。
_MAX_EXPRESSION_LEN = 200
_MAX_POW_EXPONENT = 64
# 幂运算结果位数上限。只限指数不够：`(9**64)**64` 的指数是 64，但结果是 9**4096。
_MAX_RESULT_BITS = 4096


def _check_pow(left: Any, right: Any) -> None:
    """在真正求值**之前**判幂运算是否会爆炸。

    先估位数再算，是这里唯一可行的顺序——等算出来再判就已经挂住了。
    """
    if not isinstance(right, int) or isinstance(right, bool):
        return
    if abs(right) > _MAX_POW_EXPONENT:
        raise ValueError(
            f"幂运算指数 {right} 超过上限 {_MAX_POW_EXPONENT}"
        )
    if isinstance(left, int) and not isinstance(left, bool) and abs(left) > 1:
        predicted_bits = left.bit_length() * max(right, 1)
        if predicted_bits > _MAX_RESULT_BITS:
            raise ValueError(f"幂运算结果约 {predicted_bits} 位，超过上限 {_MAX_RESULT_BITS}")


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"不支持的常量: {type(node.value).__name__}")
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
        return op(_eval_node(node.operand))
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if op is operator.pow:
            _check_pow(left, right)
        return op(left, right)
    raise ValueError(f"不支持的节点: {type(node).__name__}")


async def _calculator_handler(expression: str) -> str:
    if len(expression) > _MAX_EXPRESSION_LEN:
        return f"表达式过长（上限 {_MAX_EXPRESSION_LEN} 字符）: {expression[:_MAX_EXPRESSION_LEN]}…"
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        return f"表达式语法错误: {expression}"
    if not isinstance(tree, ast.Expression):
        return "无效表达式"
    try:
        # 丢到线程里算：这是纯 CPU 工作，直接 await 会占住事件循环（ADR-018 决策 5）。
        result = await asyncio.to_thread(_eval_node, tree.body)
        return f"{expression} = {result}"
    except Exception as exc:
        return f"计算失败: {exc}"


# ── 列出知识库文档 ───────────────────────────────────────────────────────


async def _list_documents_handler() -> str:
    # M4：经中立 port 取知识库，不再反向依赖组合根 app.deps
    from app.knowledge_access import get_knowledge_base

    try:
        docs = await get_knowledge_base().list_docs()
    except Exception as exc:
        return f"获取文档列表失败: {exc}"
    if not docs:
        return "知识库中暂无文档。"
    lines = [f"知识库文档（{len(docs)} 篇）:"]
    for doc in docs:
        title = doc.get("title") or doc.get("id") or "?"
        lines.append(f"  - {title}")
    return "\n".join(lines)


# ── 会话级笔记（session_id 由 ReActLoop 自动注入）───────────────────────

_NOTES_STORE: dict[str, list[dict[str, str]]] = {}


async def _add_note_handler(note: str, session_id: str = "") -> str:
    if not session_id:
        return "无法保存：缺少会话标识。"
    _NOTES_STORE.setdefault(session_id, [])
    _NOTES_STORE[session_id].append(
        {"time": datetime.now().astimezone().isoformat(), "note": note}
    )
    count = len(_NOTES_STORE[session_id])
    return f"已保存笔记（共 {count} 条）: {note[:100]}"


async def _get_notes_handler(session_id: str = "") -> str:
    if not session_id:
        return "无法获取：缺少会话标识。"
    entries = _NOTES_STORE.get(session_id, [])
    if not entries:
        return "暂无笔记。"
    lines = [f"笔记（{len(entries)} 条）:"]
    for i, entry in enumerate(entries, 1):
        lines.append(f"  {i}. [{entry['time']}] {entry['note']}")
    return "\n".join(lines)


# ── 注册扩展工具 ─────────────────────────────────────────────────────────

tool_registry.register(
    AgentTool(
        name="calculator",
        description="计算算术表达式，支持加减乘除、幂运算和括号",
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "要计算的算术表达式，如 2+2、3*5、(10+5)/3",
                },
            },
            "required": ["expression"],
        },
        handler=_calculator_handler,
    )
)

tool_registry.register(
    AgentTool(
        name="list_documents",
        description="列出知识库中的所有文档标题",
        parameters={"type": "object", "properties": {}},
        handler=_list_documents_handler,
    )
)

tool_registry.register(
    AgentTool(
        name="add_note",
        description="在当前会话中保存一条笔记，后续可以查看",
        parameters={
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "笔记内容",
                },
                "session_id": {
                    "type": "string",
                    "description": "会话标识（由系统自动注入）",
                },
            },
            "required": ["note"],
        },
        handler=_add_note_handler,
    )
)

tool_registry.register(
    AgentTool(
        name="get_notes",
        description="查看当前会话中保存的所有笔记",
        parameters={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "会话标识（由系统自动注入）",
                },
            },
        },
        handler=_get_notes_handler,
    )
)

__all__ = ["_NOTES_STORE"]
