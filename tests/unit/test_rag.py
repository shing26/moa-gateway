from __future__ import annotations

import json
import sys
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
from apps.code_review_pipeline.storage.review_store import (
    _embedding_dim,
    render_schema as render_review_schema,
)


# ── embeddings ──────────────────────────────────────────────────────────

def test_normalize_vector_rounds_floats() -> None:
    vector = _normalize_vector([1.0, 2.5, 3.33333])
    assert vector == (1.0, 2.5, 3.33333)
    assert all(isinstance(v, float) for v in vector)


def test_review_schema_uses_configured_embedding_dimension(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "vector_db_embedding_dim", 768)
    assert _embedding_dim() == 768
    assert render_review_schema("embedding vector(1536)", 768) == "embedding vector(768)"


def test_embedding_dimension_readers_ignore_env(monkeypatch) -> None:
    """回归（2026-09-23）：维度读取点不再各自读 env。

    此前 ``review_store._embedding_dim()`` 与 ``rag/embeddings.embedding_dimension()``
    都**反向**优先 ``CODE_REVIEW_EMBEDDING_DIM``，而 ``app/config.py`` 优先
    ``VECTOR_DB_EMBEDDING_DIM`` —— 同一组 env 可能算出不同维度，而它决定建表 DDL
    与写入向量的长度。现在唯一读取点是 config：env 在进程启动后即失效，非法值由
    config 层 fail-fast（见 test_config）。
    """
    from app.config import settings

    from apps.code_review_pipeline.rag.embeddings import embedding_dimension

    monkeypatch.setenv("CODE_REVIEW_EMBEDDING_DIM", "not-a-number")
    monkeypatch.setattr(settings, "vector_db_embedding_dim", 512)

    assert _embedding_dim() == 512, "读取点不该再看 env"
    assert embedding_dimension() == 512, "两个读取点必须同源"


def test_review_schema_allows_knowledge_vectors_without_pr() -> None:
    from pathlib import Path

    schema_path = (
        Path(__file__).resolve().parents[2]
        / "apps"
        / "code_review_pipeline"
        / "storage"
        / "schema.sql"
    )
    schema = schema_path.read_text(encoding="utf-8")
    vector_ddl = schema.split(
        "CREATE TABLE IF NOT EXISTS code_review_vectors", maxsplit=1
    )[1].split(";", maxsplit=1)[0]

    assert "REFERENCES code_review_prs" not in vector_ddl
    assert "DROP CONSTRAINT IF EXISTS code_review_vectors_trace_id_fkey" in schema
    assert "DELETE FROM code_review_vectors" in schema
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_code_review_vectors_trace_source" in schema


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
async def test_generate_embeddings_single_text_uses_string_input() -> None:
    mock_client = AsyncMock()
    mock_client.post.return_value = _make_mock_response(
        {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
    )

    results = await generate_embeddings(["hello"], client=mock_client)

    assert results[0].vector == (0.1, 0.2, 0.3)
    assert mock_client.post.call_args.kwargs["json"]["input"] == "hello"


@pytest.mark.asyncio
async def test_generate_embeddings_batch_failure_falls_back_to_single_inputs() -> None:
    class BatchRejectedResponse:
        status_code = 500

        def raise_for_status(self):
            raise Exception("batch input rejected")

    mock_client = AsyncMock()
    mock_client.post.side_effect = [
        BatchRejectedResponse(),
        _make_mock_response({"data": [{"embedding": [0.1, 0.2]}]}),
        _make_mock_response({"data": [{"embedding": [0.3, 0.4]}]}),
    ]

    results = await generate_embeddings(["hello", "world"], client=mock_client)

    assert [item.vector for item in results] == [(0.1, 0.2), (0.3, 0.4)]
    payloads = [call.kwargs["json"]["input"] for call in mock_client.post.call_args_list]
    assert payloads == [["hello", "world"], "hello", "world"]


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


def test_in_memory_vector_store_upsert_replaces_same_source() -> None:
    store = InMemoryVectorStore()
    first = EmbeddingResult(
        text="old pattern",
        vector=(1.0, 0.0, 0.0),
        source_type="pattern",
        source_id="pattern-1",
    )
    updated = EmbeddingResult(
        text="updated pattern",
        vector=(0.0, 1.0, 0.0),
        source_type="pattern",
        source_id="pattern-1",
    )

    store.upsert([first], trace_id="knowledge-base", source_type="knowledge")
    store.upsert([updated], trace_id="knowledge-base", source_type="knowledge")
    results = store.search((0.0, 1.0, 0.0), limit=5)

    assert len(results) == 1
    assert results[0]["content"] == "updated pattern"


def test_build_vector_store_returns_in_memory_when_no_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODE_REVIEW_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    store = build_vector_store()
    assert isinstance(store, InMemoryVectorStore)


def test_build_vector_store_raises_when_dsn_set_but_no_psycopg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DSN 已统一到 settings（见 _build_dsn 的说明），所以这里改它而不是设 env
    from app.config import settings

    monkeypatch.setattr(settings, "vector_db_dsn", "postgres://localhost/test")
    # Patch sys.modules, not the module attribute: build_vector_store() does a
    # FUNCTION-LOCAL `import psycopg`, so it never reads vector_store.psycopg.
    # A None entry in sys.modules makes the import itself raise ImportError.
    monkeypatch.setitem(sys.modules, "psycopg", None)
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
async def test_knowledge_base_list_docs_uses_configured_embedding_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingStore:
        def __init__(self) -> None:
            self.vector: tuple[float, ...] = ()

        def search(self, vector: tuple[float, ...], *, limit: int = 5) -> list[dict]:
            self.vector = vector
            return []

    from app.config import settings

    # 走 settings 而不是 env：维度读取点已统一到 config（见上面那条回归）
    monkeypatch.setattr(settings, "vector_db_embedding_dim", 768)
    store = RecordingStore()
    kb = KnowledgeBase(store=store)

    docs = await kb.list_docs()

    assert docs == []
    assert len(store.vector) == 768


@pytest.mark.asyncio
async def test_knowledge_base_delete_doc_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_DATABASE_URL", "")
    from apps.code_review_pipeline.rag.knowledge_base import build_knowledge_base
    kb = build_knowledge_base()
    result = await kb.delete_doc("p1")
    assert result is False
