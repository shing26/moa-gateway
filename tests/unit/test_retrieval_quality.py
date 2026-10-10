"""检索质量：gold set、离线度量、精排降级（ADR-016 / ADR-020）。

守三件事：

1. **度量算得对**。Hit@k / MRR / nDCG 的数学实现若有偏差，"先度量再谈精排"
   这条触发线就等于没有——所以用手算得出来的合成用例钉住它。
2. **精排失败不伤检索**。rerank 是增强不是前提：端点挂掉时必须保留 RRF 顺序，
   而不是让检索报错或返回空。
3. **gold set 与语料对齐**。chunk id 对不上真实分块结果的话，评测跑的是空气。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.vectordb import VectorDocument
from app.vectordb.rerank import (
    DeterministicLexicalReranker,
    HttpRerankProvider,
    NoopReranker,
    build_reranker,
)

GOLD_PATH = REPO_ROOT / "evals" / "datasets" / "retrieval_gold.jsonl"


def _docs(*ids: str) -> list[VectorDocument]:
    return [VectorDocument(id=i, content=f"content of {i}", metadata={}) for i in ids]


def _rows() -> list[dict]:
    if not GOLD_PATH.exists():
        pytest.skip("retrieval_gold.jsonl 不存在")
    return [
        json.loads(line)
        for line in GOLD_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── gold set ─────────────────────────────────────────────────────────────


def test_gold_set_schema_and_corpus_alignment() -> None:
    from evals.run_evals import load_corpus_chunks

    rows = _rows()
    assert rows, "gold set 不能为空"

    real_chunks = {f"{doc}:chunk:{index}" for doc, index, _ in load_corpus_chunks()}
    assert real_chunks, "固定语料没切出任何 chunk"

    seen: set[str] = set()
    for row in rows:
        for key in ("id", "query", "relevant_chunk_ids", "graded"):
            assert key in row, f"gold set 行缺字段 {key}: {row}"
        assert row["id"] not in seen, f"gold set id 重复: {row['id']}"
        seen.add(row["id"])
        assert row["query"].strip(), f"{row['id']} 的 query 为空"
        assert row["relevant_chunk_ids"], f"{row['id']} 没有相关 chunk"
        # 引用的 chunk 必须真的存在于固定语料里，否则评测跑的是空气
        for chunk_id in row["relevant_chunk_ids"]:
            assert chunk_id in real_chunks, f"{row['id']} 引用了不存在的 chunk {chunk_id}"
        for chunk_id, grade in row["graded"].items():
            assert isinstance(grade, int) and grade > 0, f"{row['id']} 的 {chunk_id} 分级非法"
        assert set(row["graded"]) == set(row["relevant_chunk_ids"]), (
            f"{row['id']} 的 graded 与 relevant_chunk_ids 不一致"
        )


def test_gold_set_records_annotator_provenance() -> None:
    """ADR-020 要求标注者不得与分块器作者同一人——meta 是这条的可审计证据。"""
    meta_path = GOLD_PATH.with_name("retrieval_gold.meta.json")
    if not meta_path.exists():
        pytest.skip("retrieval_gold.meta.json 不存在")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta.get("annotator"), "meta 必须写明标注者来源"
    assert meta.get("row_count", 0) > 0


# ── 度量数学 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retrieval_metrics_math_on_synthetic_cases() -> None:
    from evals.run_evals import run_retrieval_eval

    cases = [
        # 第 1 位命中：hit@1=1, mrr=1, ndcg=3/3=1.0, recall=1/1
        {"id": "a", "query": "qa", "relevant_chunk_ids": ["a"], "graded": {"a": 2}},
        # 第 3 位命中：hit@1=0, hit@3=1, mrr=1/3, ndcg=1.5/3=0.5
        {"id": "b", "query": "qb", "relevant_chunk_ids": ["b"], "graded": {"b": 2}},
        # 两个相关只召回一个：recall@5 = 1/2
        {"id": "c", "query": "qc", "relevant_chunk_ids": ["c", "d"],
         "graded": {"c": 2, "d": 1}},
    ]
    ranking = {
        "qa": ["a", "x", "y", "z", "w"],
        "qb": ["x", "y", "b", "z", "w"],
        "qc": ["c", "x", "y", "z", "w"],
    }

    async def retrieve_fn(query: str, top_k: int) -> list[str]:
        return ranking[query][:top_k]

    result = await run_retrieval_eval(cases, retrieve_fn=retrieve_fn, top_k=5)

    assert result["total"] == 3
    assert result["evaluated"] == 3
    # hit@1：a 第 1 位、c 第 1 位命中；b 在第 3 位 → 2/3
    # _ratio() 会把结果四舍五入到 4 位，所以期望值也按同一精度对齐
    assert result["hit_at_1"] == pytest.approx(round(2 / 3, 4))
    assert result["hit_at_3"] == pytest.approx(1.0)
    assert result["hit_at_5"] == pytest.approx(1.0)
    # recall@5 = (1 + 1 + 0.5) / 3
    assert result["recall_at_5"] == pytest.approx(round(2.5 / 3, 4))
    # mrr = (1 + 1/3 + 1) / 3
    assert result["mrr"] == pytest.approx(round((1 + 1 / 3 + 1) / 3, 4))
    # c 的理想 DCG = 3 + (2^1-1)/log2(3)（d 只排在第二位），
    # 所以 c 的 nDCG = 3 / (3 + 1/log2(3))，不是 1.0。
    ndcg_c = 3 / (3 + 1 / math.log2(3))
    assert result["ndcg_at_5"] == pytest.approx(round((1.0 + 0.5 + ndcg_c) / 3, 4))


@pytest.mark.asyncio
async def test_retrieval_metrics_report_skip_when_gold_set_empty() -> None:
    from evals.run_evals import run_retrieval_eval

    async def never(query: str, top_k: int) -> list[str]:
        raise AssertionError("空 gold set 不该调用 retrieve_fn")

    result = await run_retrieval_eval([], retrieve_fn=never)
    assert result["total"] == 0
    assert result["note"]


@pytest.mark.asyncio
async def test_rows_without_relevant_chunks_do_not_inflate_the_scores() -> None:
    from evals.run_evals import run_retrieval_eval

    async def retrieve_fn(query: str, top_k: int) -> list[str]:
        return ["a"]

    result = await run_retrieval_eval(
        [
            {"id": "hit", "query": "q1", "relevant_chunk_ids": ["a"], "graded": {"a": 2}},
            {"id": "none", "query": "q2", "relevant_chunk_ids": [], "graded": {}},
        ],
        retrieve_fn=retrieve_fn,
        top_k=5,
    )
    # 没有相关 chunk 的行被 continue 掉，不进分子也不进分母：
    # 若仍按 len(cases) 当分母，这条 hit_at_1 会被稀释成 0.5。
    assert result["total"] == 2
    assert result["evaluated"] == 1
    assert result["hit_at_1"] == pytest.approx(1.0)
    # 少评了一条必须写在脸上
    assert "1 行缺少" in result["note"]


# ── 精排器 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_reranker_is_identity() -> None:
    reranker = NoopReranker()
    docs = _docs("a", "b", "c")
    assert await reranker.rerank("q", docs, 2) == docs[:2]


@pytest.mark.asyncio
async def test_deterministic_lexical_reranker_orders_by_coverage() -> None:
    reranker = DeterministicLexicalReranker()
    docs = [
        VectorDocument(id="miss", content="完全无关的内容", metadata={}),
        VectorDocument(id="one", content="只提到 redis", metadata={}),
        VectorDocument(id="both", content="redis 连接地址配置", metadata={}),
    ]
    result = await reranker.rerank("redis 连接地址", docs, 2)
    # both 覆盖两个 token，one 只覆盖一个，miss 零覆盖
    assert [doc.id for doc in result] == ["both", "one"]


@pytest.mark.asyncio
async def test_deterministic_lexical_reranker_is_deterministic() -> None:
    reranker = DeterministicLexicalReranker()
    docs = _docs("b", "a", "c")
    first = [d.id for d in await reranker.rerank("anything", docs, 3)]
    second = [d.id for d in await reranker.rerank("anything", docs, 3)]
    assert first == second


@pytest.mark.asyncio
async def test_http_reranker_failure_preserves_rrf_order(monkeypatch) -> None:
    """端点挂掉：保留 RRF 顺序，绝不让检索报错。"""
    reranker = HttpRerankProvider(
        api_key="k", model="m", base_url="http://rerank.invalid", max_failures=2
    )

    async def boom(self, body):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(HttpRerankProvider, "_post", boom)
    docs = _docs("a", "b", "c")
    result = await reranker.rerank("q", docs, 3)
    assert [doc.id for doc in result] == ["a", "b", "c"]
    assert reranker.last_error is not None


@pytest.mark.asyncio
async def test_http_reranker_circuit_breaker_trips(monkeypatch) -> None:
    reranker = HttpRerankProvider(
        api_key="k", model="m", base_url="http://rerank.invalid", max_failures=2
    )

    async def boom(self, body):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(HttpRerankProvider, "_post", boom)
    assert reranker.enabled is True
    await reranker.rerank("q", _docs("a"), 1)
    await reranker.rerank("q", _docs("a"), 1)
    assert reranker.enabled is False


@pytest.mark.asyncio
async def test_http_reranker_reorders_by_relevance_score(monkeypatch) -> None:
    reranker = HttpRerankProvider(api_key="k", model="m", base_url="http://rerank.invalid")

    async def fake_post(self, body):
        return {
            "results": [
                {"index": 2, "relevance_score": 0.1},
                {"index": 0, "relevance_score": 0.9},
            ]
        }

    monkeypatch.setattr(HttpRerankProvider, "_post", fake_post)
    docs = _docs("a", "b", "c")
    result = await reranker.rerank("q", docs, 3)
    # 打分高的在前；没被打分的文档附在后面而不是丢掉
    assert result[0].id == "a"
    assert {doc.id for doc in result} == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_http_reranker_ignores_out_of_range_indices(monkeypatch) -> None:
    reranker = HttpRerankProvider(api_key="k", model="m", base_url="http://x")

    async def fake_post(self, body):
        return {"results": [{"index": 99, "relevance_score": 1.0}]}

    monkeypatch.setattr(HttpRerankProvider, "_post", fake_post)
    docs = _docs("a", "b")
    result = await reranker.rerank("q", docs, 2)
    assert [doc.id for doc in result] == ["a", "b"]


def test_build_reranker_defaults_to_noop() -> None:
    reranker = build_reranker(type("Cfg", (), {"rerank_enabled": False})())
    assert isinstance(reranker, NoopReranker)


def test_build_reranker_needs_base_url_when_enabled() -> None:
    cfg = type("Cfg", (), {"rerank_enabled": True, "rerank_base_url": ""})()
    assert isinstance(build_reranker(cfg), NoopReranker)


def test_build_reranker_uses_http_provider_when_configured() -> None:
    cfg = type(
        "Cfg",
        (),
        {
            "rerank_enabled": True,
            "rerank_base_url": "http://rerank.local",
            "rerank_api_key": "k",
            "rerank_model": "m",
            "rerank_timeout_s": 5.0,
        },
    )()
    assert isinstance(build_reranker(cfg), HttpRerankProvider)


# ── 接入点 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retriever_fetches_wider_candidates_then_truncates() -> None:
    from app.vectordb.retriever import ContextRetriever
    from app.vectordb.rerank import DeterministicLexicalReranker

    class RecordingStore:
        def __init__(self) -> None:
            self.requested_top_k = None

        async def search(self, query, top_k, filter_metadata=None):
            self.requested_top_k = top_k
            docs = _docs(*[f"d{i}" for i in range(top_k)])
            return type("R", (), {"documents": docs})()

    store = RecordingStore()
    retriever = ContextRetriever(
        store, top_k=3, reranker=DeterministicLexicalReranker()
    )  # type: ignore[arg-type]
    result = await retriever.retrieve("q", session_id="s")
    # 候选必须显著宽于 top_k，否则精排器无从挑
    assert store.requested_top_k >= 3 * 4
    # 交给 agent 的仍然是 top_k 条
    assert result.doc_count == 3


@pytest.mark.asyncio
async def test_retriever_without_reranker_does_not_widen_candidates() -> None:
    """没装精排器时按 top_k 取，不白白放大检索代价。

    pgvector 每条腿会再乘一次 ``CANDIDATE_MULTIPLIER``；retriever 先放大 4 倍，
    LIMIT 就从 20 涨到 80。返回内容其实不变（融合分降序 + doc.id 是全序），
    所以这是一个纯代价回归，测试要把它钉住。
    """
    from app.vectordb.retriever import ContextRetriever

    class RecordingStore:
        def __init__(self) -> None:
            self.requested_top_k = None

        async def search(self, query, top_k, filter_metadata=None):
            self.requested_top_k = top_k
            return type("R", (), {"documents": _docs("a", "b", "c")})()

    store = RecordingStore()
    retriever = ContextRetriever(store, top_k=3)  # type: ignore[arg-type]
    result = await retriever.retrieve("q", session_id="s")
    assert store.requested_top_k == 3
    assert result.doc_count == 3


@pytest.mark.asyncio
async def test_noop_reranker_collapses_to_absent() -> None:
    """``NoopReranker`` 必须等价于"没装精排器"，否则默认态仍然取宽候选。"""
    from app.vectordb.rerank import NoopReranker
    from app.vectordb.retriever import ContextRetriever

    class RecordingStore:
        def __init__(self) -> None:
            self.requested_top_k = None

        async def search(self, query, top_k, filter_metadata=None):
            self.requested_top_k = top_k
            return type("R", (), {"documents": _docs("a", "b", "c")})()

    store = RecordingStore()
    retriever = ContextRetriever(store, top_k=3, reranker=NoopReranker())
    assert retriever.reranker is None
    await retriever.retrieve("q", session_id="s")
    assert store.requested_top_k == 3

    # setter 走同一条折叠路径：事后把精排器换成 Noop 也要收回宽候选。
    retriever.reranker = NoopReranker()
    assert retriever.reranker is None
    await retriever.retrieve("q", session_id="s")
    assert store.requested_top_k == 3


@pytest.mark.asyncio
async def test_retriever_truncates_to_top_k_when_reranker_returns_more() -> None:
    from app.vectordb.retriever import ContextRetriever

    class GreedyReranker:
        async def rerank(self, query, docs, top_k):
            # 故意多返回：截断是接入点的责任，不能指望精排器自觉
            return list(docs) + [VectorDocument(id="extra", content="x", metadata={})]

    class Store:
        async def search(self, query, top_k, filter_metadata=None):
            return type("R", (), {"documents": _docs(*[f"d{i}" for i in range(top_k)])})()

    retriever = ContextRetriever(Store(), top_k=2, reranker=GreedyReranker())  # type: ignore[arg-type]
    result = await retriever.retrieve("q")
    assert result.doc_count == 2


# ── 错维向量必须被计数 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrong_dimension_vectors_are_counted_not_silently_dropped() -> None:
    """ADR-020 残留：错维向量只 warning 后丢弃 → 检索静默退化成纯稀疏。"""
    from app.vectordb.pgvector_client import PgVectorClient

    class WrongDimProvider:
        enabled = True
        model = "fake"

        async def embed_batch(self, texts):
            return [[0.1, 0.2, 0.3, 0.4], [0.1, 0.2], None]

    client = PgVectorClient(dsn="postgresql://unused", dim=4, embedding=WrongDimProvider())
    accepted = await client._embed_batch(["a", "b", "c"])

    assert accepted[0] == [0.1, 0.2, 0.3, 0.4]
    assert accepted[1] is None  # 错维 → 丢弃
    assert accepted[2] is None
    # 关键是**被计数**：否则"语义索引没在建"这件事无声无息
    assert client.describe()["embedding_dim_mismatches"] == 1


def test_render_schema_binds_dimension() -> None:
    from app.vectordb.pgvector_client import render_schema

    sql = "CREATE TABLE t (embedding vector(1536));"
    assert "vector(768)" in render_schema(sql, 768)
    with pytest.raises(ValueError):
        render_schema(sql, 0)


# ── 前后对比脚本 ────────────────────────────────────────────────────────


def test_compare_rerank_script_smoke(capsys) -> None:
    """脚本必须真的跑得动，并且打印的是 delta，而不是一句"精排有效"。"""
    import scripts.compare_rerank as compare

    compare.main()
    out = capsys.readouterr().out
    assert "hit_at_1" in out
    assert "delta" in out.lower()
