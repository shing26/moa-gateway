from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.agents.tools import AgentTool, ToolRegistry, tool_registry
from app.vectordb.retriever import RetrievalResult


@pytest.mark.asyncio
async def test_registry_register_and_get() -> None:
    registry = ToolRegistry()

    async def handler(query: str) -> str:
        return query

    tool = AgentTool(
        name="echo",
        description="echoes the query",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    registry.register(tool)
    assert registry.get("echo") is tool
    assert registry.get("missing") is None


@pytest.mark.asyncio
async def test_registry_register_overwrites() -> None:
    registry = ToolRegistry()

    async def handler() -> str:
        return "ok"

    registry.register(AgentTool(name="t", description="first", parameters={}, handler=handler))
    registry.register(AgentTool(name="t", description="second", parameters={}, handler=handler))
    assert registry.get("t").description == "second"


@pytest.mark.asyncio
async def test_registry_list_schemas_format() -> None:
    registry = ToolRegistry()

    async def handler(query: str) -> str:
        return query

    registry.register(
        AgentTool(
            name="my_tool",
            description="does things",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=handler,
        )
    )
    schemas = registry.list_schemas()
    assert len(schemas) == 1
    schema = schemas[0]
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "my_tool"
    assert schema["function"]["description"] == "does things"
    assert schema["function"]["parameters"]["required"] == ["query"]


def test_singleton_has_builtin_tools() -> None:
    assert tool_registry.get("knowledge_search") is not None
    assert tool_registry.get("current_time") is not None
    names = {s["function"]["name"] for s in tool_registry.list_schemas()}
    assert {"knowledge_search", "current_time"} <= names
    for schema in tool_registry.list_schemas():
        assert schema["type"] == "function"
        assert "name" in schema["function"]
        assert "description" in schema["function"]
        assert "parameters" in schema["function"]


@pytest.mark.asyncio
async def test_current_time_handler_returns_iso_local() -> None:
    tool = tool_registry.get("current_time")
    result = await tool.handler()
    parsed = datetime.fromisoformat(result)
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_knowledge_search_handler_with_mock_retriever(monkeypatch) -> None:
    fake_retriever = AsyncMock()
    fake_retriever.retrieve_knowledge = AsyncMock(
        return_value=RetrievalResult(
            chunks=["chunk one", "chunk two"],
            context="chunk one\n\n---\n\nchunk two",
            doc_count=2,
        )
    )
    monkeypatch.setattr("app.deps._retriever", fake_retriever)

    tool = tool_registry.get("knowledge_search")
    result = await tool.handler("redis config")
    assert "2" in result
    assert "chunk one" in result
    assert "chunk two" in result
    fake_retriever.retrieve_knowledge.assert_awaited_once_with("redis config")


@pytest.mark.asyncio
async def test_knowledge_search_handler_truncates_context(monkeypatch) -> None:
    fake_retriever = AsyncMock()
    fake_retriever.retrieve_knowledge = AsyncMock(
        return_value=RetrievalResult(chunks=["x" * 5000], context="x" * 5000, doc_count=1)
    )
    monkeypatch.setattr("app.deps._retriever", fake_retriever)

    tool = tool_registry.get("knowledge_search")
    result = await tool.handler("long context")
    assert result.endswith("x" * 2000)
    assert not result.endswith("x" * 2001)


def test_execute_code_registered_with_schema() -> None:
    tool = tool_registry.get("execute_code")
    assert tool is not None
    assert tool.parameters == {
        "type": "object",
        "properties": {"code": {"type": "string"}},
        "required": ["code"],
    }


@pytest.mark.asyncio
async def test_execute_code_handler_returns_approval_marker() -> None:
    tool = tool_registry.get("execute_code")
    result = await tool.handler("print('hello')\nprint('world')")
    assert result.startswith("EXECUTION_REQUIRES_APPROVAL:")
    assert "code=print('hello')\nprint('world')" in result
    assert "lines=2" in result


@pytest.mark.asyncio
async def test_execute_code_handler_truncates_code_preview() -> None:
    tool = tool_registry.get("execute_code")
    long_code = "x" * 500
    result = await tool.handler(long_code)
    assert f"code={'x' * 100}" in result
    assert "x" * 101 not in result
    assert "lines=1" in result
