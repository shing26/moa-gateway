from __future__ import annotations

import pytest

from app.knowledge import KnowledgeBase
from app.vectordb import VectorDBClient
from app.vectordb.retriever import ContextRetriever


@pytest.mark.asyncio
async def test_retrieve_knowledge_only_returns_knowledge_docs() -> None:
    client = VectorDBClient()
    kb = KnowledgeBase(client)
    await kb.add_document("doc", "alpha beta gamma")
    retriever = ContextRetriever(client)
    await retriever.store_session_context("s1", "secret session only")

    session_result = await retriever.retrieve_knowledge("secret")
    assert session_result.doc_count == 0

    knowledge_result = await retriever.retrieve_knowledge("alpha")
    assert knowledge_result.doc_count == 1
    assert "alpha" in knowledge_result.context


@pytest.mark.asyncio
async def test_retrieve_knowledge_matches_chinese_query() -> None:
    client = VectorDBClient()
    kb = KnowledgeBase(client)
    await kb.add_document(
        "redis config",
        "MOA Gateway Redis config: redis://localhost:6379/0",
    )
    retriever = ContextRetriever(client)

    result = await retriever.retrieve_knowledge("MOA网关的Redis连接地址")
    assert result.doc_count == 1
    assert "redis://localhost:6379/0" in result.context
