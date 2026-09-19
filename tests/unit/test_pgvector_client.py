"""PgVectorClient 单元测试：不连数据库，只审计它生成的 SQL。

``PgVectorClient.__init__`` 接受任意 ``pool`` 对象（``pool=object()`` 就足以让
``is_degraded`` 为 False），而 ``_run`` / ``_run_many`` 是所有 SQL 执行的唯一收口。
把这两个私有方法替换成记录器之后，就能对"发给了 PostgreSQL 什么"做精确断言，
全程不需要网络、不需要 psycopg、不需要真实实例。

本文件的重点不是"代码能跑通"，而是三件容易悄悄坏掉的事：

* 表名校验器真的挡得住注入（源码里那几个 ``# nosec`` 标记的真正依据）；
* 降级路径（无 provider / 维度不符 / provider 报错 / 池为空）返回退化结果而不抛异常；
* ``ON CONFLICT`` 分支用 COALESCE 保护已存向量，不被 NULL 覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from app.vectordb import VectorDBClient, VectorDocument, build_vector_client
from app.vectordb.pgvector_client import (
    KEYWORD_WEIGHT,
    MIN_CANDIDATES,
    VECTOR_WEIGHT,
    PgVectorClient,
    VectorStoreUnavailable,
    _validate_identifier,
    _vector_literal,
    fuse,
    split_statements,
    render_schema,
)


# ── 测试替身 ────────────────────────────────────────────────────────────────


@dataclass
class RecordedCall:
    sql: str
    params: Any
    fetch: bool


class FakeRun:
    """替换 ``PgVectorClient._run``：记录每次调用，按队列回放结果行。"""

    def __init__(self, rows=None, rowcount=1, queue=None):
        self.rows = list(rows or [])
        self.rowcount = rowcount
        self.queue = list(queue or [])
        self.calls: list[RecordedCall] = []

    async def __call__(self, sql, params, *, fetch):
        self.calls.append(RecordedCall(sql=sql, params=params, fetch=fetch))
        if not fetch:
            return self.rowcount
        return list(self.queue.pop(0)) if self.queue else list(self.rows)

    def one(self, needle: str) -> RecordedCall:
        """取唯一一条包含 ``needle`` 的调用，避免断言命中错误的语句。"""
        matched = [call for call in self.calls if needle in call.sql]
        assert len(matched) == 1, f"期望 1 条含 {needle!r} 的 SQL，实际 {len(matched)} 条"
        return matched[0]

    def none(self, needle: str) -> None:
        assert not [call for call in self.calls if needle in call.sql]


class FakeRunMany:
    """替换 ``PgVectorClient._run_many``：只记录。"""

    def __init__(self):
        self.calls: list[tuple[str, list[tuple]]] = []

    async def __call__(self, sql, rows):
        self.calls.append((sql, list(rows)))


class FakeEmbedding:
    """恒定向量的 embedding provider；``enabled=True`` 才会被真正调用。"""

    enabled = True
    model = "fake-model"

    def __init__(self, vector):
        self.vector = list(vector)
        self.calls: list[list[str]] = []

    async def embed_batch(self, texts):
        self.calls.append(list(texts))
        return [list(self.vector) for _ in texts]

    async def aclose(self):
        return None


class ExplodingEmbedding(FakeEmbedding):
    """模拟 embedding 服务故障：每次调用都抛异常。"""

    async def embed_batch(self, texts):
        self.calls.append(list(texts))
        raise RuntimeError("embedding 服务 503")


class DisabledEmbedding(FakeEmbedding):
    """模拟熔断后的 provider。"""

    enabled = False


def make_client(**kwargs) -> tuple[PgVectorClient, FakeRun, FakeRunMany]:
    """构造一个不连库的客户端：注入假池 + 替换两个 SQL 收口。"""
    kwargs.setdefault("dsn", "postgresql://user:pw@127.0.0.1:5432/gateway")
    client = PgVectorClient(pool=object(), **kwargs)
    run, many = FakeRun(), FakeRunMany()
    client._run = run
    client._run_many = many
    return client, run, many


# ── 表名校验（注入防护） ────────────────────────────────────────────────────


class TestValidateIdentifier:
    @pytest.mark.parametrize("name", ["gateway_documents", "a", "_x", "T1", "docs_2", "D"])
    def test_accepts_plain_identifiers(self, name: str):
        assert _validate_identifier(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "a b",
            "1abc",
            "docs; DROP TABLE users",
            "docs--",
            "a.b",
            "doc's",
            'doc"',
            "doc`",
            "doc\nDROP TABLE users",
            "doc/*x*/",
            "doc\\",
            "表",
            None,
        ],
    )
    def test_rejects_injection_attempts(self, name):
        with pytest.raises(ValueError):
            _validate_identifier(name)

    def test_rejection_names_the_offending_value(self):
        with pytest.raises(ValueError, match="unsafe SQL identifier"):
            _validate_identifier("docs; DROP TABLE users")

    def test_rejects_trailing_newline(self):
        # fullmatch 不允许尾随换行，避免 "gateway_documents\n" 这类输入绕过校验
        # 并污染 SQL（这也是 8 处 B608 抑制标记的兜底保证）。
        with pytest.raises(ValueError):
            _validate_identifier("gateway_documents\n")

    def test_constructor_validates_table_before_any_sql_is_built(self):
        with pytest.raises(ValueError):
            PgVectorClient(dsn="postgresql://x", table="docs; DROP TABLE users")

    def test_constructor_accepts_custom_table(self):
        client = PgVectorClient(dsn="postgresql://x", table="docs_2", pool=object())
        assert client.describe()["table"] == "docs_2"


# ── 纯函数 ──────────────────────────────────────────────────────────────────


class TestSplitStatements:
    def test_drops_comment_lines_and_empty_statements(self):
        raw = "-- leading comment\nCREATE TABLE a (id TEXT);\n  -- indented\nCREATE INDEX b;\n;\n  \n"
        assert split_statements(raw) == ["CREATE TABLE a (id TEXT)", "CREATE INDEX b"]

    def test_keeps_semicolon_free_bodies(self):
        statements = split_statements("SELECT 1; SELECT 2")
        assert statements == ["SELECT 1", "SELECT 2"]

    def test_real_schema_file_splits_into_executable_statements(self):
        from app.vectordb.pgvector_client import _SCHEMA_PATH

        raw = _SCHEMA_PATH.read_text(encoding="utf-8")
        statements = split_statements(raw)
        assert len(statements) >= 3
        assert all(statement and ";" not in statement for statement in statements)
        assert any(s.startswith("CREATE EXTENSION") for s in statements)
        assert any(s.startswith("CREATE TABLE") for s in statements)


class TestRenderSchema:
    def test_binds_configured_embedding_dimension(self):
        rendered = render_schema("embedding vector(1536)", 768)
        assert rendered == "embedding vector(768)"

    def test_rejects_non_positive_dimension(self):
        with pytest.raises(ValueError, match="positive"):
            render_schema("embedding vector(1536)", 0)


class TestFuse:
    def test_weights_are_07_vector_03_keyword(self):
        assert VECTOR_WEIGHT == pytest.approx(0.7)
        assert KEYWORD_WEIGHT == pytest.approx(0.3)
        assert VECTOR_WEIGHT + KEYWORD_WEIGHT == pytest.approx(1.0)

    def test_fuse_mixes_both_signals(self):
        assert fuse(0.5, 0.0) == pytest.approx(0.35)
        assert fuse(0.0, 1.0) == pytest.approx(0.3)
        assert fuse(1.0, 1.0) == pytest.approx(1.0)
        assert fuse(0.0, 0.0) == pytest.approx(0.0)

    def test_fuse_clamps_negative_cosine_to_zero(self):
        # pgvector 的 <=> 余弦距离域是 0..2，1 - distance 可能为负。
        assert fuse(-0.5, 0.0) == pytest.approx(0.0)
        assert fuse(-0.5, 1.0) == pytest.approx(0.3)


class TestVectorLiteral:
    def test_renders_pgvector_text_format(self):
        # 必须渲染成文本再让服务端 ::vector 转换；直接传 list 会被适配成 PG ARRAY，
        # 而 ARRAY 没有到 vector 的 cast。
        assert _vector_literal([1, 2, 3]) == "[1,2,3]"
        assert _vector_literal([1.0, 2.0, 3.0]) == "[1,2,3]"
        assert _vector_literal([0.5, -0.25]) == "[0.5,-0.25]"
        assert isinstance(_vector_literal([1.0]), str)


# ── 降级与生命周期 ──────────────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_injected_pool_keeps_client_healthy(self):
        client, _, _ = make_client()
        assert client.is_degraded is False
        assert client.degraded_reason is None
        assert client.describe()["degraded"] is False

    @pytest.mark.asyncio
    async def test_start_with_injected_pool_does_not_touch_network(self):
        client, run, _ = make_client()
        await client.start()
        assert run.calls == []
        assert client.is_degraded is False

    @pytest.mark.asyncio
    async def test_start_without_dsn_marks_degraded(self):
        client = PgVectorClient(dsn="")
        await client.start()
        assert client.is_degraded is True
        assert "VECTOR_DB_DSN" in (client.degraded_reason or "")

    @pytest.mark.asyncio
    async def test_close_releases_pool_and_marks_degraded(self):
        client, _, _ = make_client()
        await client.close()
        assert client.is_degraded is True

    @pytest.mark.asyncio
    async def test_run_raises_when_degraded(self):
        client = PgVectorClient(dsn="postgresql://x")
        with pytest.raises(VectorStoreUnavailable):
            await client._run("SELECT 1", None, fetch=True)
        with pytest.raises(VectorStoreUnavailable):
            await client._run_many("INSERT", [("a",)])

    def test_synchronous_count_is_not_supported(self):
        # 计数需要一次数据库往返，所以这里故意做成抛异常，逼调用方用 acount()。
        client, _, _ = make_client()
        with pytest.raises(NotImplementedError):
            _ = client.count


# ── search ──────────────────────────────────────────────────────────────────


class TestSearchKeywordFallback:
    @pytest.mark.asyncio
    async def test_no_provider_takes_keyword_path(self):
        client, run, _ = make_client(keyword_scan_limit=7)
        run.rows = [
            ("k1", "redis 连接超时", {"source": "knowledge"}),
            ("k2", "完全无关的内容", {"source": "knowledge"}),
        ]
        result = await client.search("redis", top_k=5, filter_metadata={"source": "knowledge"})

        assert [doc.id for doc in result.documents] == ["k1"]
        assert result.documents[0].score > 0

        call = run.one("SELECT id, content, metadata FROM")
        assert "metadata @> %s::jsonb" in call.sql
        assert "(metadata->>'searchable') IS DISTINCT FROM 'false'" in call.sql
        assert call.params[0] == json.dumps({"source": "knowledge"}, ensure_ascii=False)
        assert call.params[1] == 7
        assert call.fetch is True
        run.none("<=>")

    @pytest.mark.asyncio
    async def test_no_provider_and_no_filter_sends_empty_json_object(self):
        client, run, _ = make_client()
        await client.search("redis", top_k=3)
        assert run.one("SELECT").params[0] == "{}"

    @pytest.mark.asyncio
    async def test_disabled_provider_also_takes_keyword_path(self):
        client, run, _ = make_client(dim=4, embedding=DisabledEmbedding([0.1, 0.2, 0.3, 0.4]))
        run.rows = [("k1", "redis 连接超时", {})]
        result = await client.search("redis", top_k=5)
        assert [doc.id for doc in result.documents] == ["k1"]
        run.none("<=>")

    @pytest.mark.asyncio
    async def test_empty_query_matches_nothing(self):
        client, run, _ = make_client()
        run.rows = [("k1", "anything", {})]
        result = await client.search("", top_k=5)
        assert result.documents == []


class TestSearchVectorPath:
    @pytest.mark.asyncio
    async def test_provider_with_matching_dimension_uses_vector_candidates(self):
        provider = FakeEmbedding([0.1, 0.2, 0.3, 0.4])
        client, run, _ = make_client(dim=4, embedding=provider)
        run.queue = [
            [
                ("v1", "redis config", {"source": "knowledge"}, 0.9),
                ("v2", "redis cache", {"source": "knowledge"}, 0.8),
            ]
        ]
        result = await client.search("redis", top_k=1, filter_metadata={"source": "knowledge"})

        call = run.one("<=>")
        assert "1 - (embedding <=> %s::vector) AS score" in call.sql
        assert "ORDER BY embedding <=> %s::vector" in call.sql
        assert "embedding IS NOT NULL" in call.sql
        assert "(metadata->>'searchable') IS DISTINCT FROM 'false'" in call.sql
        assert call.params[0] == "[0.1,0.2,0.3,0.4]"
        assert call.params[2] == "[0.1,0.2,0.3,0.4]"
        assert call.params[3] == max(1 * 4, MIN_CANDIDATES)
        assert provider.calls == [["redis"]]

        # pool_best 归一化后两个候选的关键词分都是 1.0，故最终分 = 0.7*cos + 0.3
        assert [doc.id for doc in result.documents] == ["v1"]
        assert result.documents[0].score == pytest.approx(fuse(0.9, 1.0))

    @pytest.mark.asyncio
    async def test_vector_candidates_are_reordered_by_fused_score(self):
        provider = FakeEmbedding([0.1, 0.2, 0.3, 0.4])
        client, run, _ = make_client(dim=4, embedding=provider)
        # v2 向量分更高，但 v1 关键词命中更多：融合后 v1 应反超。
        run.queue = [
            [
                ("v2", "redis", {"source": "knowledge"}, 0.95),
                ("v1", "redis redis redis", {"source": "knowledge"}, 0.80),
            ]
        ]
        result = await client.search("redis", top_k=2)
        assert [doc.id for doc in result.documents] == ["v1", "v2"]
        assert result.documents[0].score == pytest.approx(fuse(0.80, 1.0))
        assert result.documents[1].score == pytest.approx(fuse(0.95, 1 / 3))

    @pytest.mark.asyncio
    async def test_wrong_dimensionality_falls_back_to_keyword_without_raising(self):
        provider = FakeEmbedding([0.1, 0.2, 0.3])  # 3 != dim 4
        client, run, _ = make_client(dim=4, embedding=provider)
        run.rows = [("k1", "redis 连接超时", {})]
        result = await client.search("redis", top_k=5)
        assert [doc.id for doc in result.documents] == ["k1"]
        run.none("<=>")

    @pytest.mark.asyncio
    async def test_provider_raising_falls_back_to_keyword_without_raising(self):
        provider = ExplodingEmbedding([0.1, 0.2, 0.3, 0.4])
        client, run, _ = make_client(dim=4, embedding=provider)
        run.rows = [("k1", "redis 连接超时", {})]
        result = await client.search("redis", top_k=5)
        assert [doc.id for doc in result.documents] == ["k1"]
        run.none("<=>")

    @pytest.mark.asyncio
    async def test_empty_vector_result_falls_back_to_keyword(self):
        provider = FakeEmbedding([0.1, 0.2, 0.3, 0.4])
        client, run, _ = make_client(dim=4, embedding=provider)
        run.queue = [[], [("k1", "redis 连接超时", {})]]
        result = await client.search("redis", top_k=5)
        assert len(run.calls) == 2
        assert [doc.id for doc in result.documents] == ["k1"]

    @pytest.mark.asyncio
    async def test_degraded_pool_returns_empty_result_without_raising(self):
        client = PgVectorClient(dsn="")
        result = await client.search("redis", top_k=5)
        assert result.documents == []
        assert isinstance(result.documents, list)

    @pytest.mark.asyncio
    async def test_top_up_skips_ids_already_returned(self):
        provider = FakeEmbedding([0.1, 0.2, 0.3, 0.4])
        client, run, _ = make_client(dim=4, embedding=provider)
        run.queue = [
            [("v1", "redis alpha", {"source": "knowledge"}, 0.9)],
            [
                ("v1", "redis alpha", {"source": "knowledge"}),
                ("k1", "redis beta", {"source": "knowledge"}),
            ],
        ]
        result = await client.search("redis", top_k=3)
        assert [doc.id for doc in result.documents] == ["v1", "k1"]
        assert len(run.calls) == 2

    @pytest.mark.asyncio
    async def test_topped_up_rows_use_comparable_scales_and_stay_sorted(self):
        """补齐行必须与向量候选统一量纲并参与最终排序。

        修复前：融合分恒在 0..1，关键词补充分是"词元命中次数"（无上界），
        两者直接拼表且不重排，导致 documents 非降序、调用方取 documents[0] 出错。
        修复后：以覆盖全部候选的 pool_best 做归一化，最终按 score 降序。
        """
        provider = FakeEmbedding([0.1, 0.2, 0.3, 0.4])
        client, run, _ = make_client(dim=4, embedding=provider)
        run.queue = [
            [("v1", "redis", {"source": "knowledge"}, 0.9)],
            [("k1", "redis redis redis", {"source": "knowledge"})],
        ]
        result = await client.search("redis", top_k=2)

        # 返回顺序必须按相关度降序，且每条分数都在 0..1 的同一量纲内。
        scores = [doc.score for doc in result.documents]
        assert scores == sorted(scores, reverse=True)
        assert all(0.0 <= s <= 1.0 for s in scores)
        # v1 有向量（余弦 0.9）+ 关键词，k1 只有关键词，因此 v1 应排在前。
        assert [doc.id for doc in result.documents] == ["v1", "k1"]


# ── 写入 ────────────────────────────────────────────────────────────────────


class TestUpsert:
    @pytest.mark.asyncio
    async def test_upsert_batch_sql_guards_existing_embeddings(self):
        client, _, many = make_client(dim=3, embedding=FakeEmbedding([1.0, 2.0, 3.0]))
        await client.upsert_batch(
            [VectorDocument(id="d1", content="你好", metadata={"source": "knowledge", "title": "标题"})]
        )

        sql, rows = many.calls[0]
        assert "INSERT INTO gateway_documents (id, content, metadata, embedding)" in sql
        assert "VALUES (%s, %s, %s::jsonb, %s::vector)" in sql
        assert "ON CONFLICT (id) DO UPDATE" in sql
        # 关键：embedding 冲突时不得被 NULL 覆盖，否则一次 embedding 故障就会
        # 静默抹掉已建好的语义索引。
        assert "embedding = COALESCE(EXCLUDED.embedding, gateway_documents.embedding)" in sql
        assert "updated_at = now()" in sql

        doc_id, content, metadata, vector = rows[0]
        assert (doc_id, content) == ("d1", "你好")
        assert isinstance(vector, str) and vector == "[1,2,3]"
        assert json.loads(metadata) == {"source": "knowledge", "title": "标题"}

    @pytest.mark.asyncio
    async def test_upsert_delegates_to_batch(self):
        client, _, many = make_client()
        await client.upsert(VectorDocument(id="d1", content="x"))
        assert len(many.calls) == 1
        assert many.calls[0][1][0][0] == "d1"

    @pytest.mark.asyncio
    async def test_empty_batch_issues_no_sql(self):
        client, run, many = make_client()
        await client.upsert_batch([])
        assert run.calls == [] and many.calls == []

    @pytest.mark.asyncio
    async def test_embedding_failure_still_writes_rows_with_null_vector(self):
        provider = ExplodingEmbedding([1.0, 2.0, 3.0])
        client, _, many = make_client(dim=3, embedding=provider)
        await client.upsert_batch(
            [VectorDocument(id="d1", content="a"), VectorDocument(id="d2", content="b")]
        )
        rows = many.calls[0][1]
        assert [row[0] for row in rows] == ["d1", "d2"]
        assert all(row[3] is None for row in rows)
        assert all(row[1] for row in rows)

    @pytest.mark.asyncio
    async def test_no_provider_writes_rows_with_null_vector(self):
        client, _, many = make_client(dim=3)
        await client.upsert_batch([VectorDocument(id="d1", content="a")])
        assert many.calls[0][1][0][3] is None


# ── 读取与删除 ──────────────────────────────────────────────────────────────


class TestReads:
    @pytest.mark.asyncio
    async def test_delete_by_metadata_returns_int_and_passes_filter_json(self):
        client, run, _ = make_client()
        run.rowcount = 7
        assert await client.delete_by_metadata({"doc_id": "abc"}) == 7

        call = run.one("DELETE FROM gateway_documents WHERE metadata @> %s::jsonb")
        assert call.params == (json.dumps({"doc_id": "abc"}, ensure_ascii=False),)
        assert call.fetch is False

    @pytest.mark.asyncio
    async def test_delete_by_metadata_treats_none_rowcount_as_zero(self):
        client, run, _ = make_client()
        run.rowcount = None
        assert await client.delete_by_metadata({"doc_id": "abc"}) == 0

    @pytest.mark.asyncio
    async def test_delete_by_metadata_accepts_non_ascii_values(self):
        client, run, _ = make_client()
        await client.delete_by_metadata({"title": "标题"})
        assert json.loads(run.calls[0].params[0]) == {"title": "标题"}

    @pytest.mark.asyncio
    async def test_get_maps_row_to_document(self):
        client, run, _ = make_client()
        run.queue = [[("d1", "content", {"source": "knowledge"})]]
        doc = await client.get("d1")
        assert doc == VectorDocument(
            id="d1", content="content", metadata={"source": "knowledge"}
        )
        assert run.one("WHERE id = %s").params == ("d1",)

    @pytest.mark.asyncio
    async def test_get_returns_none_when_absent(self):
        client, run, _ = make_client()
        assert await client.get("missing") is None

    @pytest.mark.asyncio
    async def test_get_defaults_null_metadata_to_empty_dict(self):
        client, run, _ = make_client()
        run.queue = [[("d1", "content", None)]]
        assert (await client.get("d1")).metadata == {}

    @pytest.mark.asyncio
    async def test_find_by_metadata_maps_rows_and_honours_limit(self):
        client, run, _ = make_client()
        run.queue = [[("a", "x", {"source": "knowledge"}), ("b", "y", None)]]
        docs = await client.find_by_metadata({"source": "knowledge"}, limit=5)
        assert [doc.id for doc in docs] == ["a", "b"]
        assert docs[1].metadata == {}
        call = run.one("LIMIT %s")
        assert call.params == (
            json.dumps({"source": "knowledge"}, ensure_ascii=False),
            5,
        )

    @pytest.mark.asyncio
    async def test_find_by_metadata_returns_empty_list_without_rows(self):
        client, run, _ = make_client()
        assert await client.find_by_metadata({"source": "knowledge"}) == []

    @pytest.mark.asyncio
    async def test_acount_returns_int(self):
        client, run, _ = make_client()
        run.queue = [[(42,)]]
        assert await client.acount() == 42

    @pytest.mark.asyncio
    async def test_acount_returns_zero_without_rows(self):
        client, run, _ = make_client()
        assert await client.acount() == 0

    @pytest.mark.asyncio
    async def test_clear_deletes_every_row(self):
        client, run, _ = make_client()
        await client.clear()
        assert run.one("DELETE FROM gateway_documents").params is None


# ── 后端选择 ────────────────────────────────────────────────────────────────


class _Settings:
    vector_db_dsn = ""
    vector_db_table = "gateway_documents"
    vector_db_embedding_dim = 1536
    vector_db_pool_min_size = 1
    vector_db_pool_max_size = 4
    vector_db_keyword_scan_limit = 2000
    vector_db_strict = False
    vector_db_auto_migrate = True
    embedding_api_key = ""
    embedding_model = "text-embedding-3-small"
    embedding_base_url = ""
    embedding_timeout_s = 10.0


class TestBuildVectorClient:
    def test_empty_dsn_selects_in_memory_backend(self):
        client = build_vector_client(_Settings())
        assert isinstance(client, VectorDBClient)
        assert client.backend == "memory"

    def test_dsn_selects_postgres_backend_without_connecting(self):
        settings = _Settings()
        settings.vector_db_dsn = "postgresql://u:p@127.0.0.1:5432/gateway"
        client = build_vector_client(settings)
        assert isinstance(client, PgVectorClient)
        assert client.backend == "postgres"
        assert client.describe()["table"] == "gateway_documents"
        assert client.describe()["dim"] == 1536
        # 构造阶段不发连接，start() 之前就是降级态。
        assert client.describe()["degraded"] is True

    def test_healthz_style_describe_is_json_serialisable(self):
        import json as _json

        client, _, _ = make_client(dim=8, embedding=FakeEmbedding([0.1] * 8))
        payload = client.describe()
        assert _json.loads(_json.dumps(payload))["backend"] == "postgres"
        assert payload["embedding"] is True
        assert payload["embedding_model"] == "fake-model"
