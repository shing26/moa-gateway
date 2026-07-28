from __future__ import annotations
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.deps import _retriever
import logging

logger = logging.getLogger("moa.routes.health")
router = APIRouter()

@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}

@router.get("/healthz")
async def healthz() -> dict[str, object]:
    checks = {}
    redis_check = "unknown"
    try:
        from app.redis_state.store import RedisConfig, RedisStateStore
        store = RedisStateStore(RedisConfig(url="redis://localhost:6379/0"))
        client = await store.connect()
        pong = await client.ping()
        if pong:
            redis_check = "fallback_memory" if store.is_fallback else "connected"
        await store.close()
    except Exception as e:
        redis_check = "error: " + str(e)[:50]
    checks["redis"] = redis_check
    healthy_values = {"connected", "ok", "healthy", "fallback_memory"}
    all_healthy = all(v in healthy_values for v in checks.values())
    return {"status": "healthy" if all_healthy else "degraded", "checks": checks}

@router.delete("/api/v1/privacy/user/{user_id}")
async def privacy_erase(user_id: str) -> JSONResponse:
    deleted = {}
    try:
        count = await _retriever._client.delete_by_metadata({"user_id": user_id})
        deleted["vectordb"] = count
    except Exception as e:
        deleted["vectordb"] = str(e)
    logger.info("privacy erase user=%s deleted=%s", user_id, deleted)
    return JSONResponse({"user_id": user_id, "deleted": deleted, "status": "ok"})
