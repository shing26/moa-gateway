from __future__ import annotations
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.config import settings
from app.deps import _retriever, pipeline, vector_client
from app.middleware.auth import insecure_mode_enabled
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
        store = RedisStateStore(RedisConfig(url=settings.redis_url))
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
    # 引擎不是健康项（没有"坏值"），所以放在 checks 之外，避免污染
    # all_healthy 的取值集合。
    describe = getattr(pipeline, "describe", None)
    engine_name = str(describe().get("engine", "fsm")) if callable(describe) else "fsm"
    # 审批链路的状态必须可见（2026-09-28）：`HITL_ENABLED` 的默认值是 **false**
    # （app/config.py），而审批人白名单为空时走 fail-closed——"审批根本没生效"
    # 和"谁能批"这两件事此前在运行时完全看不见，只能去读 .env 才知道。
    # 放在 checks 之外：它们是**配置事实**，不是健康项，不该把 status 拉成 degraded。
    if not settings.hitl_enabled:
        hitl_state = "disabled(HITL_ENABLED=false)：输出不会被挂起等人工"
    elif settings.hitl_approver_ids:
        hitl_state = f"enabled(审批人白名单 {len(settings.hitl_approver_ids)} 人)"
    elif insecure_mode_enabled(settings.gateway_allow_insecure):
        hitl_state = "enabled(⚠️ 无白名单且 GATEWAY_ALLOW_INSECURE=1：能看到卡片的人都能批)"
    else:
        hitl_state = "enabled(⚠️ 无白名单且非 insecure：所有审批都会被拒)"
    result = {
        "status": "healthy" if all_healthy else "degraded",
        "checks": checks,
        "engine": engine_name,
        "hitl": hitl_state,
    }
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
