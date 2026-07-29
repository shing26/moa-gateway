from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import logging
from app.deps import knowledge_base

logger = logging.getLogger("moa.routes.knowledge")
router = APIRouter()

class UploadRequest(BaseModel):
    title: str
    content: str

@router.post("/knowledge/upload")
async def upload_doc(req: UploadRequest) -> JSONResponse:
    if not req.title or not req.content:
        return JSONResponse({"error": "title and content required"}, status_code=400)
    doc_id = await knowledge_base.add_document(req.title, req.content)
    return JSONResponse({"id": doc_id, "title": req.title, "status": "ok"})

@router.get("/knowledge/list")
async def list_docs() -> JSONResponse:
    docs = await knowledge_base.list_docs()
    return JSONResponse({"documents": docs, "total": len(docs)})

@router.delete("/knowledge/{doc_id}")
async def delete_doc(doc_id: str) -> JSONResponse:
    ok = await knowledge_base.delete_doc(doc_id)
    if not ok:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({"status": "deleted"})
