from __future__ import annotations
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.deps import _retriever, vector_client
import logging
import time
from typing import Any

logger = logging.getLogger("moa.routes.health")
router = APIRouter()
_healthz_cache: dict[str, Any] = {"at": 0.0, "result": None}
_HEALTHZ_TTL = 5.0

@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}

@router.get("/healthz")
async def healthz() -> dict[str, object]:
    now = time.monotonic()
    if _healthz_cache["result"] is not None and now - _healthz_cache["at"] < _HEALTHZ_TTL:
        return dict(_healthz_cache["result"])
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
    # 检索后端必须可见：静默退回内存存储会让"数据其实没落盘"这件事无人知晓。
    vector_info = vector_client.describe()
    checks["vectordb"] = (
        f"degraded: {str(vector_info.get('reason') or 'unknown')[:60]}"
        if vector_info.get("degraded")
        else str(vector_info.get("backend", "unknown"))
    )
    # "memory" 是受支持的零配置模式，与 redis 的 fallback_memory 同级；
    # 只有 "degraded: ..." 才代表配置与实际不符，需要告警。
    healthy_values = {"connected", "ok", "healthy", "fallback_memory", "memory", "postgres"}
    all_healthy = all(v in healthy_values for v in checks.values())
    result = {"status": "healthy" if all_healthy else "degraded", "checks": checks}
    _healthz_cache["at"] = now
    _healthz_cache["result"] = result
    return result

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
