from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("moa.agents.tools")


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Awaitable[str]]


class ToolRegistry:
    def __init__(
        self,
        workspace_factory: Callable[[Path], "ToolRegistry"] | None = None,
    ) -> None:
        self._tools: dict[str, AgentTool] = {}
        self._workspace_factory = workspace_factory

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

    def for_workspace(self, workspace: Path | str) -> "ToolRegistry":
        """复制同一工具定义，但把所有文件操作重新绑定到另一个工作区。

        并发 worker 不能共享闭包里的工作区根目录；该入口由 ``build_toolset``
        注入，普通注册表（例如 planner 的空工具表）调用时明确报错。
        """
        if self._workspace_factory is None:
            raise TypeError("该 ToolRegistry 不支持绑定到其他工作区")
        return self._workspace_factory(Path(workspace))


async def _knowledge_search_handler(query: str) -> str:
    # M4：经中立 port 取检索器，不再反向依赖组合根 app.deps
    from app.knowledge_access import get_retriever

    result = await get_retriever().retrieve_knowledge(query)
    context = result.context[:2000]
    logger.debug("knowledge_search: query=%s docs=%d", query[:50], result.doc_count)
    return f"\u6765\u6e90\u6587\u6863\u6570: {result.doc_count}\n\n{context}"


async def _current_time_handler() -> str:
    return datetime.now().astimezone().isoformat()


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

__all__ = ["AgentTool", "ToolRegistry", "tool_registry"]
