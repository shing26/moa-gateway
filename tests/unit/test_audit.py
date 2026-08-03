from __future__ import annotations

import tempfile
import pytest

from app.audit.models import AuditEntry
from app.audit.wal import AsyncWal, LogConfig
from app.middleware.request_logger import log_request
from app.vectordb import VectorDBClient, VectorDocument, VectorSearchResult
from app.vectordb.retriever import ContextRetriever


class TestAsyncWal:
    @pytest.mark.asyncio
    async def test_append_and_size(self):
        wal = AsyncWal()
        entry = AuditEntry(
            trace_id="t1", session_id="s1", agent_name="coder",
            agent_output="hello", intent="coding", eval_score=1.0,
        )
        await wal.append(entry)
        assert wal.size == 1

    @pytest.mark.asyncio
    async def test_replay_returns_batch(self):
        wal = AsyncWal()
        for i in range(5):
            await wal.append(AuditEntry(
                trace_id=f"t{i}", session_id="s1", agent_name="coder",
                agent_output=f"out{i}", intent="coding", eval_score=1.0,
            ))
        batch = await wal.replay(batch_size=3)
        assert len(batch) == 3
        assert wal.size == 2

    @pytest.mark.asyncio
    async def test_replay_all_clears_buffer(self):
        wal = AsyncWal()
        await wal.append(AuditEntry(
            trace_id="t1", session_id="s1", agent_name="coder",
            agent_output="x", intent="coding", eval_score=1.0,
        ))
        entries = await wal.replay_all()
        assert len(entries) == 1
        assert wal.size == 0

    @pytest.mark.asyncio
    async def test_estimated_bytes(self):
        wal = AsyncWal()
        await wal.append(AuditEntry(
            trace_id="t1", session_id="s1", agent_name="coder",
            agent_output="hello world", intent="coding", eval_score=1.0,
        ))
        assert wal.estimated_bytes > 0

    @pytest.mark.asyncio
    async def test_replay_empty_returns_empty(self):
        wal = AsyncWal()
        assert await wal.replay_all() == []

    @pytest.mark.asyncio
    async def test_log_request_stores_input_output_previews(self, monkeypatch, tmp_path):
        wal = AsyncWal(_config=LogConfig(directory=str(tmp_path), retention_days=90))
        monkeypatch.setattr("app.middleware.request_logger._wal", wal)

        class FakeRequest:
            method = "POST"
            url = "http://test/webhook/feishu"

        await log_request(
            FakeRequest(), 200, 12.3, "s1", "general", "search", "allow",
            "用户输入内容", "模型输出内容",
        )
        entries = await wal.replay_all()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.extra["input_preview"] == "用户输入内容"
        assert entry.extra["output_preview"] == "模型输出内容"
        assert entry.agent_output == "模型输出内容"

        files = list(tmp_path.glob("audit-*.jsonl"))
        assert files
        line = files[0].read_text(encoding="utf-8")
        assert "用户输入内容" in line
        assert "模型输出内容" in line
        assert '"status": 200' in line
        assert '"duration_ms": 12.3' in line


class TestVectorDBClient:
    @pytest.mark.asyncio
    async def test_upsert_and_search(self):
        db = VectorDBClient()
        await db.upsert(VectorDocument(id="doc1", content="Python is a programming language", metadata={"lang": "python"}))
        await db.upsert(VectorDocument(id="doc2", content="Java is also a language", metadata={"lang": "java"}))
        result = await db.search("python")
        assert len(result.documents) == 1
        assert result.documents[0].id == "doc1"

    @pytest.mark.asyncio
    async def test_upsert_batch(self):
        db = VectorDBClient()
        docs = [
            VectorDocument(id="a", content="doc a"),
            VectorDocument(id="b", content="doc b"),
        ]
        await db.upsert_batch(docs)
        assert db.count == 2

    @pytest.mark.asyncio
    async def test_search_with_metadata_filter(self):
        db = VectorDBClient()
        await db.upsert(VectorDocument(id="d1", content="user message", metadata={"session_id": "s1"}))
        await db.upsert(VectorDocument(id="d2", content="other message", metadata={"session_id": "s2"}))
        result = await db.search("message", filter_metadata={"session_id": "s1"})
        assert len(result.documents) == 1
        assert result.documents[0].id == "d1"

    @pytest.mark.asyncio
    async def test_search_matches_chinese_bigrams(self):
        db = VectorDBClient()
        await db.upsert(VectorDocument(id="d1", content="MOA Gateway Redis config: redis://localhost:6379/0"))
        await db.upsert(VectorDocument(id="d2", content="Python is a programming language"))
        result = await db.search("MOA网关的Redis连接地址")
        assert len(result.documents) == 1
        assert result.documents[0].id == "d1"
        assert result.documents[0].score > 0

    @pytest.mark.asyncio
    async def test_delete_by_metadata(self):
        db = VectorDBClient()
        await db.upsert(VectorDocument(id="d1", content="x", metadata={"user_id": "u1"}))
        await db.upsert(VectorDocument(id="d2", content="y", metadata={"user_id": "u2"}))
        deleted = await db.delete_by_metadata({"user_id": "u1"})
        assert deleted == 1
        assert db.count == 1

    @pytest.mark.asyncio
    async def test_get_by_id(self):
        db = VectorDBClient()
        await db.upsert(VectorDocument(id="doc1", content="content"))
        doc = await db.get("doc1")
        assert doc is not None
        assert doc.content == "content"

    @pytest.mark.asyncio
    async def test_clear(self):
        db = VectorDBClient()
        await db.upsert(VectorDocument(id="a", content="x"))
        await db.clear()
        assert db.count == 0

    @pytest.mark.asyncio
    async def test_search_empty_db(self):
        db = VectorDBClient()
        result = await db.search("anything")
        assert len(result.documents) == 0


class TestContextRetriever:
    @pytest.mark.asyncio
    async def test_retrieve_returns_chunks(self):
        db = VectorDBClient()
        await db.upsert(VectorDocument(id="d1", content="relevant info", metadata={"session_id": "s1"}))
        retriever = ContextRetriever(db, top_k=5)
        result = await retriever.retrieve("relevant", session_id="s1")
        assert result.doc_count == 1
        assert "relevant info" in result.context

    @pytest.mark.asyncio
    async def test_store_session_context(self):
        db = VectorDBClient()
        retriever = ContextRetriever(db)
        await retriever.store_session_context("s1", "session data", {"key": "val"})
        assert db.count == 1

    @pytest.mark.asyncio
    async def test_search_no_match(self):
        db = VectorDBClient()
        retriever = ContextRetriever(db)
        result = await retriever.retrieve("nonexistent_content_xyz", session_id="s99")
        assert result.doc_count == 0
        assert result.context == ""
