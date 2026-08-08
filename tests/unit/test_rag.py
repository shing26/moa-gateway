from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from apps.code_review_pipeline.rag.embeddings import (
    EmbeddingError,
    EmbeddingResult,
    _normalize_vector,
    generate_embeddings,
)
from apps.code_review_pipeline.rag.knowledge_base import KnowledgeDoc, KnowledgeBase
from apps.code_review_pipeline.rag.retriever import RetrievalResult, retrieve_team_patterns
from apps.code_review_pipeline.rag.vector_store import (
    InMemoryVectorStore,
    PgVectorStore,
    VectorStoreInitError,
    build_vector_store,
)


# ── embeddings ──────────────────────────────────────────────────────────

def test_normalize_vector_rounds_floats() -> None:
    vector = _normalize_vector([1.0, 2.5, 3.33333])
    assert vector == (1.0, 2.5, 3.33333)
    assert all(isinstance(v, float) for v in vector)


def _make_mock_response(data):
    class MockResponse:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            return data
    return MockResponse()


@pytest.mark.asyncio
async def test_generate_embeddings_returns_results() -> None:
    mock_response = {
        "data": [
            {"embedding": [0.1, 0.2, 0.3]},
            {"embedding": [0.4, 0.5, 0.6]},
        ]
    }

    mock_client = AsyncMock()
    mock_client.post.return_value = _make_mock_response(mock_response)

    results = await generate_embeddings(["hello", "world"], client=mock_client)
    assert len(results) == 2
    assert results[0].text == "hello"
    assert results[0].vector == (0.1, 0.2, 0.3)
    assert results[0].source_type == "embedding"
    assert results[1].text == "world"
    assert results[1].vector == (0.4, 0.5, 0.6)


@pytest.mark.asyncio
async def test_generate_embeddings_empty_input_returns_empty() -> None:
    results = await generate_embeddings([])
    assert results == []


@pytest.mark.asyncio
async def test_generate_embeddings_raises_on_failure() -> None:
    class FailingResponse:
        status_code = 500
        def raise_for_status(self):
            raise Exception("server error")

    mock_client = AsyncMock()
    mock_client.post.return_value = FailingResponse()

    with pytest.raises(EmbeddingError):
        await generate_embeddings(["hello"], client=mock_client)


# ── vector_store ────────────────────────────────────────────────────────

def test_in_memory_vector_store_upsert_and_search() -> None:
    store = InMemoryVectorStore()
    items = [
        EmbeddingResult(
            text="doc A",
            vector=(1.0, 0.0, 0.0),
            source_type="pr",
            source_id="pr-1",
        ),
        EmbeddingResult(
            text="doc B",
            vector=(0.0, 1.0, 0.0),
            source_type="pr",
            source_id="pr-2",
        ),
    ]
    store.upsert(items, trace_id="trace-1", source_type="pr")
    results = store.search((1.0, 0.0, 0.0), limit=2)
    assert len(results) == 2
    assert results[0]["source_id"] == "pr-1"
    assert results[0]["score"] > results[1]["score"]


def test_in_memory_vector_store_search_empty() -> None:
    store = InMemoryVectorStore()
    results = store.search((1.0, 0.0, 0.0), limit=5)
    assert results == []


def test_build_vector_store_returns_in_memory_when_no_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODE_REVIEW_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    store = build_vector_store()
    assert isinstance(store, InMemoryVectorStore)


def test_build_vector_store_raises_when_dsn_set_but_no_psycopg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_REVIEW_DATABASE_URL", "postgres://localhost/test")
    monkeypatch.setattr("apps.code_review_pipeline.rag.vector_store.psycopg", None, raising=False)
    with pytest.raises(VectorStoreInitError):
        build_vector_store()


# ── retriever ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retrieve_team_patterns_empty_query_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_DATABASE_URL", "")

    from app.agents.contract import AgentEnvelope
    envelope = AgentEnvelope(
        trace_id="t1",
        session_id="s1",
        user_raw_input="",
        global_summary="",
        agent_local_slot={},
        history=(),
    )
    result = await retrieve_team_patterns(envelope)
    assert result.context == ""
    assert result.chunks == []
    assert result.doc_count == 0


@pytest.mark.asyncio
async def test_retrieve_team_patterns_returns_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_DATABASE_URL", "")

    mock_store = InMemoryVectorStore()
    mock_store.upsert(
        [
            EmbeddingResult(
                text="Team pattern: use redis.asyncio",
                vector=(0.1, 0.2, 0.3),
                source_type="pattern",
                source_id="pattern-1",
            )
        ],
        trace_id="knowledge-base",
        source_type="knowledge",
    )

    from app.agents.contract import AgentEnvelope
    envelope = AgentEnvelope(
        trace_id="t1",
        session_id="s1",
        user_raw_input="redis lock",
        global_summary="",
        agent_local_slot={"diff": "redis.set(...)"},
        history=(),
    )

    with patch("apps.code_review_pipeline.rag.retriever.build_vector_store", return_value=mock_store):
        with patch(
            "apps.code_review_pipeline.rag.retriever.generate_embeddings",
            return_value=[EmbeddingResult(text="query", vector=(0.1, 0.2, 0.3), source_type="emb", source_id="e1")],
        ):
            result = await retrieve_team_patterns(envelope)

    assert result.doc_count == 1
    assert "团队历史相似案例/规范" in result.context
    assert len(result.chunks) == 1


# ── knowledge_base ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_knowledge_base_ingest_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_DATABASE_URL", "")
    store = InMemoryVectorStore()
    kb = KnowledgeBase(store=store)

    docs = [
        KnowledgeDoc(
            source_type="pattern",
            source_id="p1",
            title="Redis Lock Pattern",
            content="Always set timeout on redis locks",
        )
    ]

    with patch(
        "apps.code_review_pipeline.rag.knowledge_base.generate_embeddings",
        return_value=[EmbeddingResult(text="Always set timeout on redis locks", vector=(0.1, 0.2, 0.3), source_type="emb", source_id="e1")],
    ):
        count = await kb.ingest_documents(docs)

    assert count == 1
    results = store.search((0.1, 0.2, 0.3), limit=5)
    assert len(results) == 1
    assert results[0]["source_id"] == "p1"


@pytest.mark.asyncio
async def test_knowledge_base_list_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_DATABASE_URL", "")
    store = InMemoryVectorStore()
    kb = KnowledgeBase(store=store)
    docs = await kb.list_docs()
    assert docs == []


@pytest.mark.asyncio
async def test_knowledge_base_delete_doc_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_DATABASE_URL", "")
    from apps.code_review_pipeline.rag.knowledge_base import build_knowledge_base
    kb = build_knowledge_base()
    result = await kb.delete_doc("p1")
    assert result is False
