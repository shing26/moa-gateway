from __future__ import annotations
import logging, uuid
from datetime import datetime, timezone
from typing import Any
from app.vectordb import VectorDocument

logger = logging.getLogger("moa.knowledge")

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

# 文档目录项（manifest）的 source 标识，与分片行（source="knowledge"）区分。
# manifest 带 searchable=False，因此永远不会作为检索结果返回给上下文。
MANIFEST_SOURCE = "knowledge_manifest"

# 详情页最多展示的分片数，防止超大文档把接口拖垮。
MAX_CHUNKS_PER_DOC = 10000


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if size <= 0:
        raise ValueError(f"chunk_text: size 必须 > 0，收到 {size}")
    # overlap >= size 时步长非正，窗口不再前进，循环永不退出直至 OOM。
    if overlap >= size:
        raise ValueError(f"chunk_text: overlap ({overlap}) 必须小于 size ({size})")
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start += size - overlap
    return chunks


def manifest_id(doc_id: str) -> str:
    return f"{doc_id}:manifest"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeBase:
    """Knowledge documents stored entirely in the vector store.

    Previously this kept a second, in-process dict of document metadata
    alongside the chunk rows. That was two sources of truth for the same fact:
    with a persistent backend a restart would leave chunks searchable while the
    document list came back empty -- strictly worse than today, where both
    vanish together and the inconsistency is at least obvious.

    Now there is one source of truth. Each document writes:

      * N chunk rows   -> source="knowledge", the retrievable corpus
      * 1 manifest row -> source="knowledge_manifest", searchable=False

    The manifest holds title, full text, creation time and chunk count, so
    listing is a single metadata query and needs no chunk reads.
    """

    def __init__(self, vector_db: Any) -> None:
        self._db = vector_db

    async def add_document(self, title: str, content: str, doc_id: str | None = None) -> str:
        doc_id = doc_id or uuid.uuid4().hex[:12]
        chunks = chunk_text(content)
        # doc_id 同时挂在分片行和 manifest 行上，一次删除即可清理干净。
        await self._db.delete_by_metadata({"doc_id": doc_id})

        docs = [
            VectorDocument(
                id=f"{doc_id}:chunk:{i}",
                content=chunk,
                metadata={"source": "knowledge", "doc_id": doc_id, "title": title, "chunk": i},
            )
            for i, chunk in enumerate(chunks)
        ]
        docs.append(
            VectorDocument(
                id=manifest_id(doc_id),
                content=content,
                metadata={
                    "source": MANIFEST_SOURCE,
                    "doc_id": doc_id,
                    "title": title,
                    "created_at": _now_iso(),
                    "chunk_count": len(chunks),
                    "searchable": False,
                },
            )
        )
        await self._db.upsert_batch(docs)
        logger.info("knowledge doc added: %s (%d chunks)", doc_id, len(chunks))
        return doc_id

    async def list_docs(self) -> list[dict]:
        manifests = await self._db.find_by_metadata({"source": MANIFEST_SOURCE}, limit=1000)
        manifests.sort(key=lambda doc: (doc.metadata.get("created_at", ""), doc.id))
        return [
            {
                "id": doc.metadata.get("doc_id", ""),
                "title": doc.metadata.get("title", ""),
                # 列表页把 chunks 当数量用（dashboard.js:299）。
                "chunks": int(doc.metadata.get("chunk_count", 0)),
            }
            for doc in manifests
        ]

    async def get_doc(self, doc_id: str) -> dict | None:
        manifest = await self._db.get(manifest_id(doc_id))
        if manifest is None:
            return None
        # 详情页把 chunks 当字符串数组用（dashboard.js:342-351），必须还原真实分片。
        rows = await self._db.find_by_metadata(
            {"source": "knowledge", "doc_id": doc_id}, limit=MAX_CHUNKS_PER_DOC
        )
        rows.sort(key=lambda doc: int(doc.metadata.get("chunk", 0)))
        return {
            "id": manifest.metadata.get("doc_id", doc_id),
            "title": manifest.metadata.get("title", ""),
            "content": manifest.content,
            "chunks": [doc.content for doc in rows],
            "created_at": manifest.metadata.get("created_at", ""),
        }

    async def delete_doc(self, doc_id: str) -> bool:
        manifest = await self._db.get(manifest_id(doc_id))
        if manifest is None:
            return False
        await self._db.delete_by_metadata({"doc_id": doc_id})
        logger.info("knowledge doc deleted: %s", doc_id)
        return True
