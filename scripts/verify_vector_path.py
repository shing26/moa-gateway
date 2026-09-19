"""向量检索真实路径验证：pgvector + 本地 Ollama embedding + 熔断降级。

用法（需先启动 moa-pgvector 容器）：
    .venv/Scripts/python.exe scripts/verify_vector_path.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 先让 .env 生效，再只补缺省值；这样本机端口调整不会被脚本硬编码覆盖。
load_dotenv()
os.environ.setdefault(
    "VECTOR_DB_DSN",
    "postgresql://gateway:gateway@localhost:5433/gateway",
)
# 注意：不能用 VECTOR_DB_TABLE 自定义表名 —— db/gateway_schema.sql 硬编码
# gateway_documents，自定义表名会导致建表/查询不一致（已记录为产品 bug）
os.environ.setdefault("VECTOR_DB_EMBEDDING_DIM", "768")  # nomic-embed-text 输出 768 维
os.environ.setdefault("EMBEDDING_API_KEY", "ollama")  # 本地端点不校验非空值
os.environ.setdefault("EMBEDDING_BASE_URL", "http://localhost:11434/v1")
os.environ.setdefault("EMBEDDING_MODEL", "nomic-embed-text:latest")

from app.config import settings  # noqa: E402
from app.vectordb import build_vector_client  # noqa: E402


DOCS = [
    # 三条语义可分的内容 + 一条关键词陷阱（包含查询字面词但语义无关）
    ("emb-doc-1", "数据库连接池的作用是复用已建立的连接，避免频繁握手带来的开销", {"source": "knowledge"}),
    ("emb-doc-2", "向量数据库通常使用 HNSW 索引来加速近似最近邻检索", {"source": "knowledge"}),
    ("emb-doc-3", "Python 的 GIL 使得多线程无法并行执行 CPU 密集型任务", {"source": "knowledge"}),
    ("emb-doc-trap", "检索系统索引索引这个词出现在无关语境：超市的商品索引目录", {"source": "knowledge"}),
]

# 改写式查询：与 doc-2 语义相近但字面几乎不重叠，关键词打分应输给向量腿
QUERY = "做相似搜索的时候该挑什么数据结构"


async def test_real_vector_path() -> bool:
    client = build_vector_client()
    try:
        backend = getattr(client, "backend", "?")
        print(f"[1] 后端选择: {backend}")
        if backend != "postgres":
            print(f"    FAIL: 期望 postgres(pgvector) 后端，实际 {backend}")
            return False

        await client.start()
        if getattr(client, "_degraded_reason", None):
            print(f"    FAIL: 后端启动失败，已降级: {client._degraded_reason}")
            return False

        for doc_id, content, meta in DOCS:
            await client.upsert(
                __import__("app.vectordb", fromlist=["VectorDocument"]).VectorDocument(
                    id=doc_id, content=content, metadata=meta
                )
            )
        print(f"[2] 已入库 {len(DOCS)} 条文档（含 1 条关键词陷阱）")

        result = await client.search(QUERY, top_k=3)
        top = result.documents[0] if result.documents else None
        print(f"[3] 查询: {QUERY!r}")
        for i, d in enumerate(result.documents):
            print(f"    #{i + 1} score={d.score:.4f} id={d.id} content={d.content[:24]}...")

        if top is None:
            print("    FAIL: 无检索结果")
            return False
        ids = [d.id for d in result.documents]
        if "emb-doc-2" not in ids:
            print("    FAIL: 语义检索结果里没有 HNSW 文档，向量腿可能未生效")
            return False
        if ids.index("emb-doc-2") > ids.index("emb-doc-trap"):
            print("    FAIL: 关键词陷阱排在语义命中之前，融合排序异常")
            return False
        print("    PASS: 向量腿生效（语义命中排名高于关键词陷阱）")
        print("    NOTE: top1 语义质量受 nomic-embed-text 中文能力限制，生产建议换多语言模型")
        return True
    finally:
        await client.close()


async def test_circuit_breaker_fallback() -> bool:
    """embedding 端点不可达时：检索不得崩溃，应降级关键词并仍能返回结果。"""
    import logging

    logging.getLogger("moa.vectordb").setLevel(logging.CRITICAL)  # 静音预期的报错日志

    # build_embedding_provider 在构建时读取 settings 属性，patch 单例即可
    real_url = settings.embedding_base_url
    settings.embedding_base_url = "http://localhost:59999/v1"  # 死端口
    broken_client = build_vector_client()
    try:
        await broken_client.start()
        result = await broken_client.search("连接池 复用", top_k=2)
    finally:
        await broken_client.close()
        settings.embedding_base_url = real_url

    docs = result.documents
    print("[4] 熔断降级: embedding 指向死端口后 search 不崩溃")
    if not docs:
        print("    FAIL: 关键词回退没有返回任何结果")
        return False
    print(f"    PASS: 关键词回退返回 {len(docs)} 条，top1={docs[0].id}")
    return True


async def main() -> int:
    ok1 = await test_real_vector_path()
    print()
    ok2 = await test_circuit_breaker_fallback()
    print()
    print(f"结论: 真实向量路径={'PASS' if ok1 else 'FAIL'} 熔断降级={'PASS' if ok2 else 'FAIL'}")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg AsyncConnectionPool 不支持 Windows 默认 ProactorEventLoop。
        sys.exit(asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop))
    sys.exit(asyncio.run(main()))
