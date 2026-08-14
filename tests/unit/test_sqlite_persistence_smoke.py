"""Verify persisted SQLite vector store can be loaded and searched after 'restart'."""
from __future__ import annotations

import os

from apps.code_review_pipeline.rag.vector_store import InMemoryVectorStore


def test_sqlite_persistence_loads_and_searches() -> None:
    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..",
        "data",
        "code_review_vectors.sqlite",
    )
    db_path = os.path.normpath(db_path)

    assert os.path.exists(db_path), f"SQLITE_FILE_MISSING: {db_path}"

    store = InMemoryVectorStore(db_path=db_path)
    assert len(store._items) > 0, "NO_ITEMS_LOADED"

    first = store._items[0]
    assert first["source_id"]
    assert first["trace_id"]

    query_vector = first.get("vector", ())
    results = store.search(query_vector, limit=3)
    assert len(results) > 0

    top = results[0]
    assert top["source_id"] == first["source_id"]
    assert top["score"] > 0.0
