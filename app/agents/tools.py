from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("moa.agents.tools")


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Awaitable[str]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def list_schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]


async def _knowledge_search_handler(query: str) -> str:
    from app.deps import _retriever

    result = await _retriever.retrieve_knowledge(query)
    context = result.context[:2000]
    logger.debug("knowledge_search: query=%s docs=%d", query[:50], result.doc_count)
    return f"\u6765\u6e90\u6587\u6863\u6570: {result.doc_count}\n\n{context}"


async def _current_time_handler() -> str:
    return datetime.now().astimezone().isoformat()


async def _execute_code_handler(code: str) -> str:
    code_preview = code[:100]
    lines = code.count("\n") + 1
    logger.warning("execute_code requires approval: code=%s lines=%d", code_preview, lines)
    return f"EXECUTION_REQUIRES_APPROVAL: code={code_preview} lines={lines}"


tool_registry = ToolRegistry()
tool_registry.register(
    AgentTool(
        name="knowledge_search",
        description="\u68c0\u7d22\u77e5\u8bc6\u5e93\u4e2d\u4e0e\u67e5\u8be2\u76f8\u5173\u7684\u6587\u6863\u7247\u6bb5",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "\u8981\u68c0\u7d22\u77e5\u8bc6\u5e93\u7684\u67e5\u8be2\u8bed\u53e5",
                },
            },
            "required": ["query"],
        },
        handler=_knowledge_search_handler,
    )
)
tool_registry.register(
    AgentTool(
        name="current_time",
        description="\u83b7\u53d6\u5f53\u524d\u672c\u5730\u65f6\u95f4\uff08ISO 8601 \u683c\u5f0f\uff09",
        parameters={"type": "object", "properties": {}},
        handler=_current_time_handler,
    )
)
tool_registry.register(
    AgentTool(
        name="execute_code",
        description="\u6267\u884c\u4e00\u6bb5\u4ee3\u7801\uff08\u9ad8\u5371\u64cd\u4f5c\uff0c\u9700\u4eba\u5de5\u5ba1\u6279\uff09",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
            },
            "required": ["code"],
        },
        handler=_execute_code_handler,
    )
)

__all__ = ["AgentTool", "ToolRegistry", "tool_registry"]
