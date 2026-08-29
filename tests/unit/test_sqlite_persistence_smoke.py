"""Verify persisted SQLite vector store can be loaded and searched after 'restart'."""
from __future__ import annotations

from apps.code_review_pipeline.rag.embeddings import EmbeddingResult
from apps.code_review_pipeline.rag.vector_store import InMemoryVectorStore


def test_sqlite_persistence_loads_and_searches(tmp_path) -> None:
    db_path = str(tmp_path / "code_review_vectors.sqlite")

    store = InMemoryVectorStore(db_path=db_path)
    store.upsert(
        [
            EmbeddingResult(
                text="def add(a, b): return a + b",
                vector=(0.1, 0.2, 0.3, 0.4),
                source_type="github_pr",
                source_id="octocat/Hello-World#1",
                metadata={"severity": "info"},
            )
        ],
        trace_id="trace-001",
        source_type="github_pr",
    )
    store.close()

    reloaded = InMemoryVectorStore(db_path=db_path)
    assert len(reloaded._items) == 1, "NO_ITEMS_LOADED"

    first = reloaded._items[0]
    assert first["source_id"] == "octocat/Hello-World#1"
    assert first["trace_id"] == "trace-001"

    query_vector = first.get("vector", ())
    results = reloaded.search(query_vector, limit=3)
    assert len(results) > 0

    top = results[0]
    assert top["source_id"] == first["source_id"]
    assert top["score"] > 0.0
    reloaded.close()
