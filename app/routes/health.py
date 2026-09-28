from __future__ import annotations
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.config import settings
from app.deps import _retriever, pipeline, vector_client
from app.middleware.auth import insecure_mode_enabled
import logging
import pathlib
import time
from typing import Any

logger = logging.getLogger("moa.routes.health")
router = APIRouter()
_healthz_cache: dict[str, Any] = {"at": 0.0, "result": None}
_HEALTHZ_TTL = 5.0

# 审计链自检的文件大小上限：/healthz 挂在请求路径上（5 秒缓存一次），不该在这里做
# 全量文件 IO。超大文件交给 CLI（scripts/verify_audit_chain.py）。
_CHAIN_CHECK_MAX_BYTES = 8 * 1024 * 1024


def _audit_chain_check() -> dict[str, str]:
    """校验**最新一个**审计文件的哈希链（ADR-019 那条回路的末端之一）。

    链在此前**没有任何消费者**——写出来却没人跑，"篡改可发现"就没有回路末端。
    这里和 CLI 一起构成末端：healthz 只回答"最近这个文件有没有被动过"且代价封顶，
    全量校验是 CLI 的活。

    返回 ``{check, detail}``：``check`` 进 ``checks`` 参与健康判定（``ok`` 在
    ``healthy_values`` 里，``degraded: …`` 不在，于是断裂会把 status 拉成 degraded）；
    ``detail`` 是给人看的一句话，放在 ``checks`` 之外。
    """
    from app.audit.wal import verify_audit_dir

    directory = pathlib.Path(settings.log_dir)
    files = sorted(directory.glob("audit-*.jsonl"))
    if not files:
        return {"check": "ok", "detail": "无审计文件"}
    newest = files[-1]
    try:
        size = newest.stat().st_size
    except OSError as exc:
        return {"check": "ok", "detail": f"读不到 {newest.name}（{exc}）"}
    if size > _CHAIN_CHECK_MAX_BYTES:
        return {
            "check": "ok",
            "detail": (
                f"{newest.name} 过大（{size // (1024 * 1024)}MB），"
                "请用 scripts/verify_audit_chain.py 全量校验"
            ),
        }
    report = verify_audit_dir(directory, limit=1)
    if not report:
        return {"check": "ok", "detail": "无审计文件"}
    name, (line, chained) = next(iter(report.items()))
    if chained == 0:
        # 链上线（2026-09-28）**之前**写的文件没有哈希字段 —— 无链可校验。
        # 说成"断裂"会让信号永远红（永远红和永远绿一样没人看）；说成"完整"则是假的。
        return {"check": "ok", "detail": f"{name} 无链可校验（链上线前的数据）"}
    if line is not None:
        return {
            "check": f"degraded: 审计链断裂 {name}:{line}",
            "detail": f"{name} 第 {line} 行起无法证明未被改动",
        }
    return {"check": "ok", "detail": f"{name} 链完整（{chained} 行带链）"}

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
    dim_mismatch = int(vector_info.get("embedding_dim_mismatches") or 0)
    if dim_mismatch:
        # 模型返回的维度与表不符 → 向量被逐条丢弃、稠密腿永远空、检索静默退化成纯稀疏。
        # 这**是健康问题**（语义索引实际上没在建立），不能算正常；启动时的表维度校验
        # 抓不到它（配置与表一致，错的是模型），所以由这个计数兜住（2026-09-28）。
        checks["vectordb"] = (
            f"degraded: {dim_mismatch} 条 embedding 维度与表不符、已丢弃（语义索引未建立）"
        )
    elif vector_info.get("degraded"):
        checks["vectordb"] = f"degraded: {str(vector_info.get('reason') or 'unknown')[:60]}"
    else:
        checks["vectordb"] = str(vector_info.get("backend", "unknown"))
    check_chain = _audit_chain_check()
    checks["audit_chain"] = check_chain["check"]
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
        "audit_chain": check_chain["detail"],
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
