"""KnowledgeBase 在两种后端上的行为一致性测试。

这次改动的全部意义就是"换后端不改调用方"，所以这组断言在同一个 fixture 上跑两遍：
内存 ``VectorDBClient`` 和 ``PgVectorClient``。后者没有真实数据库，用 ``FakePgStore``
替换掉 ``_run`` / ``_run_many`` 两个 SQL 收口，并按真实语义解释它收到的语句
（``metadata @>``、``searchable`` 排除、``COALESCE`` 冲突语义、余弦排序），
这样断言的仍是"KnowledgeBase + PgVectorClient 生成的 SQL"的组合行为，
而不是把后端整个架空。

另一个重点是钉死 ``chunks`` 的多态：列表页要 int，详情页要 list[str]。
两处形状由 dashboard.js 的不同渲染方式决定，合并它们会静默打坏前端。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest

from app.knowledge import KnowledgeBase, chunk_text, manifest_id
from app.vectordb import VectorDBClient, VectorDocument
from app.vectordb.pgvector_client import PgVectorClient

LONG_TEXT = (
    "MOA Gateway 的 Redis 连接地址是 redis://localhost:6379/0，"
    "超时时间由 REDIS_TIMEOUT 控制，连接池上限由 REDIS_POOL_MAX 控制。"
    + "这一段是填充内容，用来把文档撑过单个分片的长度限制。" * 18
)


# ── PostgreSQL 替身 ─────────────────────────────────────────────────────────


@dataclass
class StoredRow:
    id: str
    content: str
    metadata: dict
    embedding: list[float] | None


def _parse_vector(literal: str) -> list[float]:
    return [float(part) for part in literal.strip("[]").split(",")]


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm_l = math.sqrt(sum(a * a for a in left))
    norm_r = math.sqrt(sum(b * b for b in right))
    if not norm_l or not norm_r:
        return 0.0
    return dot / (norm_l * norm_r)


def _contains(metadata: dict, filter_metadata: dict) -> bool:
    """等价 PostgreSQL 的 ``metadata @> %s::jsonb``."""
    return all(metadata.get(key) == value for key, value in filter_metadata.items())


def _is_searchable(metadata: dict) -> bool:
    """等价 ``(metadata->>'searchable') IS DISTINCT FROM 'false'``."""
    return metadata.get("searchable") is not False


class FakePgStore:
    """按真实 SQL 语义执行 PgVectorClient 发出的语句（仅实现测试所需语句）。"""

    def __init__(self, client: PgVectorClient):
        self.rows: dict[str, StoredRow] = {}
        self.sql: list[str] = []
        client._run = self.run
        client._run_many = self.run_many

    # -- SELECT ------------------------------------------------------------
    async def run(self, sql: str, params, *, fetch: bool):
        self.sql.append(sql)
        text = " ".join(sql.split())

        if text.startswith("SELECT count(*)"):
            return [(len(self.rows),)]

        if "<=>" in text:
            return self._vector_candidates(params)

        if "WHERE id = %s" in text:
            row = self.rows.get(params[0])
            return [] if row is None else [(row.id, row.content, row.metadata)]

        if "IS DISTINCT FROM 'false'" in text:  # 关键词回退扫描
            filter_metadata = json.loads(params[0])
            return [
                (row.id, row.content, row.metadata)
                for row in self.rows.values()
                if _contains(row.metadata, filter_metadata) and _is_searchable(row.metadata)
            ][: params[1]]

        if text.startswith("SELECT") and "WHERE metadata @>" in text:  # find_by_metadata
            filter_metadata = json.loads(params[0])
            return [
                (row.id, row.content, row.metadata)
                for row in self.rows.values()
                if _contains(row.metadata, filter_metadata)
            ][: params[1]]

        if text.startswith("DELETE FROM"):
            if "WHERE metadata @" in text:
                filter_metadata = json.loads(params[0])
                victims = [
                    row_id
                    for row_id, row in self.rows.items()
                    if _contains(row.metadata, filter_metadata)
                ]
            else:
                victims = list(self.rows)
            for row_id in victims:
                del self.rows[row_id]
            return len(victims)

        raise AssertionError(f"替身未实现的 SQL: {text}")

    def _vector_candidates(self, params) -> list[tuple]:
        literal, filter_json, _literal, limit = params
        query = _parse_vector(literal)
        filter_metadata = json.loads(filter_json)
        scored = [
            (row, _cosine(query, row.embedding))
            for row in self.rows.values()
            if row.embedding is not None
            and _contains(row.metadata, filter_metadata)
            and _is_searchable(row.metadata)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [
            (row.id, row.content, row.metadata, similarity)
            for row, similarity in scored[:limit]
        ]

    # -- INSERT ... ON CONFLICT --------------------------------------------
    async def run_many(self, sql: str, rows):
        self.sql.append(sql)
        assert "ON CONFLICT (id) DO UPDATE" in sql
        assert "COALESCE(EXCLUDED.embedding" in sql
        for row_id, content, metadata_json, vector_literal in rows:
            embedding = _parse_vector(vector_literal) if vector_literal is not None else None
            existing = self.rows.get(row_id)
            # 复刻 SQL 里的 COALESCE：新向量为 NULL 时保留旧向量。
            if embedding is None and existing is not None:
                embedding = existing.embedding
            self.rows[row_id] = StoredRow(row_id, content, json.loads(metadata_json), embedding)


# ── fixture ─────────────────────────────────────────────────────────────────


@pytest.fixture(params=["memory", "postgres"])
def backend(request) -> Any:
    """同一组断言跑在两个后端上：接口等价就是这次改动要证明的东西。"""
    if request.param == "memory":
        return VectorDBClient()

    client = PgVectorClient(
        dsn="postgresql://user:pw@127.0.0.1:5432/gateway",
        pool=object(),
        keyword_scan_limit=500,
    )
    FakePgStore(client)
    return client


async def row_count(db) -> int:
    if isinstance(db, PgVectorClient):
        return await db.acount()
    return db.count


# ── 写入与列表 ──────────────────────────────────────────────────────────────


class TestAddDocument:
    @pytest.mark.asyncio
    async def test_list_docs_reports_chunk_count_as_int(self, backend):
        kb = KnowledgeBase(backend)
        doc_id = await kb.add_document("部署手册", LONG_TEXT)
        expected = len(chunk_text(LONG_TEXT))
        assert expected > 1, "LONG_TEXT 必须跨多个分片，否则本测试没有意义"

        entry = next(doc for doc in await kb.list_docs() if doc["id"] == doc_id)
        # 列表页按数量渲染（dashboard.js:299），必须是 int。
        assert type(entry["chunks"]) is int
        assert entry["chunks"] == expected

    @pytest.mark.asyncio
    async def test_manifest_row_carries_directory_metadata(self, backend):
        kb = KnowledgeBase(backend)
        doc_id = await kb.add_document("部署手册", LONG_TEXT)

        manifest = await backend.get(manifest_id(doc_id))
        assert manifest is not None
        assert manifest.metadata["source"] == "knowledge_manifest"
        assert manifest.metadata["doc_id"] == doc_id
        assert manifest.metadata["title"] == "部署手册"
        assert manifest.metadata["chunk_count"] == len(chunk_text(LONG_TEXT))
        assert manifest.metadata["searchable"] is False
        assert manifest.content == LONG_TEXT

    @pytest.mark.asyncio
    async def test_chunk_rows_are_stored_with_source_knowledge(self, backend):
        kb = KnowledgeBase(backend)
        doc_id = await kb.add_document("部署手册", LONG_TEXT)
        rows = await backend.find_by_metadata({"source": "knowledge", "doc_id": doc_id})
        assert len(rows) == len(chunk_text(LONG_TEXT))
        assert {int(row.metadata["chunk"]) for row in rows} == set(
            range(len(chunk_text(LONG_TEXT)))
        )
        assert all(row.metadata["title"] == "部署手册" for row in rows)


class TestGetDoc:
    @pytest.mark.asyncio
    async def test_chunks_is_a_list_of_strings_matching_the_chunker(self, backend):
        kb = KnowledgeBase(backend)
        doc_id = await kb.add_document("部署手册", LONG_TEXT)

        doc = await kb.get_doc(doc_id)
        assert doc is not None
        # 详情页按字符串数组渲染（dashboard.js:342-351），必须是 list[str]。
        assert isinstance(doc["chunks"], list)
        assert all(isinstance(chunk, str) and chunk for chunk in doc["chunks"])
        # 顺序按 chunk 下标还原，与 chunk_text 的输出逐项相等。
        assert doc["chunks"] == chunk_text(LONG_TEXT)
        assert all(chunk in LONG_TEXT for chunk in doc["chunks"])

    @pytest.mark.asyncio
    async def test_content_is_the_full_original_text(self, backend):
        kb = KnowledgeBase(backend)
        doc_id = await kb.add_document("部署手册", LONG_TEXT)
        doc = await kb.get_doc(doc_id)
        assert doc["content"] == LONG_TEXT

    @pytest.mark.asyncio
    async def test_created_at_is_a_non_empty_iso_timestamp(self, backend):
        kb = KnowledgeBase(backend)
        doc_id = await kb.add_document("部署手册", LONG_TEXT)
        created_at = (await kb.get_doc(doc_id))["created_at"]

        assert created_at, "created_at 曾经被硬编码为空字符串"
        parsed = datetime.fromisoformat(created_at)
        assert parsed.year >= 2024
        assert parsed.tzinfo is not None

    @pytest.mark.asyncio
    async def test_get_doc_returns_none_for_unknown_id(self, backend):
        kb = KnowledgeBase(backend)
        assert await kb.get_doc("nope") is None


# ── 检索 ────────────────────────────────────────────────────────────────────


class TestSearchExcludesManifest:
    @pytest.mark.asyncio
    async def test_query_spanning_a_chunk_boundary_never_returns_the_manifest(self, backend):
        """构造一个只有全文命中、分片都命不中的查询。

        120 字符的标记跨越 index 899 处的分片缝隙：分片 1 覆盖 [450,950) 只拿到前 51
        个字符，分片 2 覆盖 [900,1119) 又缺了开头，于是没有任何分片包含完整标记。
        唯一命中的是 manifest 行 —— 它必须被 searchable=False 挡住。
        """
        token = "z" * 120
        text = "a" * 899 + token + "b" * 100
        kb = KnowledgeBase(backend)
        doc_id = await kb.add_document("边界文档", text)

        assert len(chunk_text(text)) == 3
        assert not any(token in chunk for chunk in chunk_text(text))

        manifest = await backend.get(manifest_id(doc_id))
        assert manifest is not None and token in manifest.content, "前提：manifest 确实命中"

        result = await backend.search(token, top_k=5)
        assert result.documents == [], "manifest 行泄漏进了检索结果"

    @pytest.mark.asyncio
    async def test_search_returns_only_chunk_rows(self, backend):
        kb = KnowledgeBase(backend)
        await kb.add_document("部署手册", LONG_TEXT)

        result = await backend.search("redis", top_k=10)
        assert result.documents
        assert all(doc.metadata["source"] == "knowledge" for doc in result.documents)
        assert all(doc.id.endswith(":chunk:0") or ":chunk:" in doc.id for doc in result.documents)
        assert all(not doc.id.endswith(":manifest") for doc in result.documents)
        assert all(doc.score > 0 for doc in result.documents)


# ── 删除与覆盖 ──────────────────────────────────────────────────────────────


class TestDeleteAndReplace:
    @pytest.mark.asyncio
    async def test_delete_doc_removes_manifest_and_every_chunk(self, backend):
        kb = KnowledgeBase(backend)
        doc_id = await kb.add_document("部署手册", LONG_TEXT)
        assert await row_count(backend) == len(chunk_text(LONG_TEXT)) + 1

        assert await kb.delete_doc(doc_id) is True
        assert await kb.get_doc(doc_id) is None
        assert await kb.list_docs() == []
        assert await row_count(backend) == 0, "manifest 或分片行没有被删干净"
        assert await backend.find_by_metadata({"doc_id": doc_id}) == []

    @pytest.mark.asyncio
    async def test_delete_unknown_doc_returns_false(self, backend):
        kb = KnowledgeBase(backend)
        assert await kb.delete_doc("nope") is False

    @pytest.mark.asyncio
    async def test_delete_doc_twice_returns_false_the_second_time(self, backend):
        kb = KnowledgeBase(backend)
        doc_id = await kb.add_document("部署手册", LONG_TEXT)
        assert await kb.delete_doc(doc_id) is True
        assert await kb.delete_doc(doc_id) is False

    @pytest.mark.asyncio
    async def test_readding_same_doc_id_leaves_no_stale_chunks(self, backend):
        kb = KnowledgeBase(backend)
        doc_id = await kb.add_document("部署手册", LONG_TEXT)
        first_count = len(chunk_text(LONG_TEXT))
        assert first_count > 1

        await kb.add_document("部署手册 v2", "短文档。", doc_id=doc_id)

        doc = await kb.get_doc(doc_id)
        assert doc["title"] == "部署手册 v2"
        assert doc["chunks"] == ["短文档。"]
        assert doc["content"] == "短文档。"
        # 1 个分片 + 1 个 manifest，旧分片一个都不能留下。
        assert await row_count(backend) == 2

        listed = await kb.list_docs()
        assert len(listed) == 1
        assert listed[0]["chunks"] == 1

    @pytest.mark.asyncio
    async def test_readding_same_doc_id_with_more_chunks_grows_cleanly(self, backend):
        kb = KnowledgeBase(backend)
        doc_id = await kb.add_document("v1", "短文档。")
        await kb.add_document("v2", LONG_TEXT, doc_id=doc_id)

        doc = await kb.get_doc(doc_id)
        assert doc["chunks"] == chunk_text(LONG_TEXT)
        assert await row_count(backend) == len(chunk_text(LONG_TEXT)) + 1


# ── 两个后端共有的不变式 ────────────────────────────────────────────────────


class TestBackendParity:
    @pytest.mark.asyncio
    async def test_both_backends_expose_the_same_search_result_type(self, backend):
        from app.vectordb import VectorSearchResult

        kb = KnowledgeBase(backend)
        await kb.add_document("部署手册", LONG_TEXT)
        result = await backend.search("redis", top_k=3)
        assert isinstance(result, VectorSearchResult)
        assert all(isinstance(doc, VectorDocument) for doc in result.documents)

    @pytest.mark.asyncio
    async def test_both_backends_report_corpus_size_after_delete(self, backend):
        kb = KnowledgeBase(backend)
        first = await kb.add_document("a", LONG_TEXT)
        second = await kb.add_document("b", "另一篇文档。")
        assert len(await kb.list_docs()) == 2

        await kb.delete_doc(first)
        remaining = await kb.list_docs()
        assert [doc["id"] for doc in remaining] == [second]
