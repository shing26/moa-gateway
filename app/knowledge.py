from __future__ import annotations
import logging, hashlib, uuid
from dataclasses import dataclass
from typing import Any
from app.vectordb import VectorDocument

logger = logging.getLogger("moa.knowledge")

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

@dataclass
class KnowledgeDoc:
    id: str
    title: str
    content: str
    chunks: list[str]
    created_at: str

def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start += size - overlap
    return chunks

class KnowledgeBase:
    def __init__(self, vector_db: Any) -> None:
        self._db = vector_db
        self._docs: dict[str, KnowledgeDoc] = {}

    async def add_document(self, title: str, content: str) -> str:
        doc_id = uuid.uuid4().hex[:12]
        chunks = chunk_text(content)
        doc = KnowledgeDoc(id=doc_id, title=title, content=content, chunks=chunks, created_at="")
        self._docs[doc_id] = doc
        for i, chunk in enumerate(chunks):
            cid = f"{doc_id}:chunk:{i}"
            await self._db.upsert(VectorDocument(
                id=cid, content=chunk,
                metadata={"source": "knowledge", "doc_id": doc_id, "title": title, "chunk": i},
            ))
        logger.info("knowledge doc added: %s (%d chunks)", doc_id, len(chunks))
        return doc_id

    async def list_docs(self) -> list[dict]:
        return [{"id": d.id, "title": d.title, "chunks": len(d.chunks)} for d in self._docs.values()]

    async def delete_doc(self, doc_id: str) -> bool:
        if doc_id not in self._docs:
            return False
        del self._docs[doc_id]
        await self._db.delete_by_metadata({"doc_id": doc_id})
        logger.info("knowledge doc deleted: %s", doc_id)
        return True
