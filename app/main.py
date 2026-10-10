from __future__ import annotations
import json
import logging, os, pathlib
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry import trace
import app.agents.loader
from app.deps import (
    _card_sender, _feishu_config, _flag_client, engine, es_writer, logger,
    init_feishu, init_prompts, obsidian_sync, tracer, vector_client,
)
from app.fsm.state_machine import InvalidStateTransitionException
from app.models.errors import ErrorCode, MoaError, http_status_for
from app.observability.tracing import setup_tracing, TraceConfig
from app.middleware.auth import AuthMiddleware, insecure_mode_enabled
from app.middleware.flags import FeatureFlagMiddleware
from app.config import settings
from app.routes.chat import router as chat_router
from app.routes.collab import router as collab_router
from app.routes.dashboard import router as dashboard_router
from app.routes.feishu import router as feishu_router
from app.routes.health import router as health_router
from app.routes.webhook import webhook_router
from app.routes.knowledge import router as knowledge_router
from apps.code_review_pipeline.routing.github_review_route import github_review_router


# 网关此前**从不配置根 logger**：root level 是默认的 WARNING 且没有任何 handler，
# 于是全仓的 `logger.info(...)` 都被静默丢弃。后果不是"日志少几行"，而是**专门为
# 定位某个故障而写的判据从未生效过**——典型如卡片回调的
# `card_action 已受理 action=... elapsed_ms=...`：2026-09-29 加它就是为了把
# "服务端超时"和"网络往返吃窗口"分开，而它一次都没出现过，于是那条判断一直只能靠猜。
#
# `app/worker.py` 一直有 basicConfig，所以 worker 的日志是全的、网关的不是——
# 这个不一致正是"网关日志里什么都查不到"的来源。
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global tracer
    try:
        cfg = TraceConfig(otlp_endpoint=settings.otel_exporter_otlp_endpoint)
        setup_tracing(cfg)
    except Exception:
        logger.warning("opentelemetry tracing init failed")
    init_feishu()
    init_prompts()
    # 先开存储：obsidian_sync 会往知识库写入。DSN 未配置时这是空操作。
    await vector_client.start()
    await obsidian_sync.start()
    tracer = trace.get_tracer("moa-gateway")
    yield
    logger.info("moa gateway shutting down")
    if es_writer is not None:
        await es_writer.aclose()
    await obsidian_sync.close()
    await vector_client.close()
    engine.session_store.clear_all()
    _flag_client.invalidate()


app = FastAPI(title="Agent Gateway", version="0.1.0", lifespan=lifespan)
STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
app.mount("/dashboard/static", StaticFiles(directory=STATIC_DIR), name="dashboard-static")
app.include_router(dashboard_router)
app.include_router(chat_router)
app.include_router(collab_router)
app.include_router(feishu_router)
app.include_router(health_router)
app.include_router(webhook_router)
app.include_router(knowledge_router)
app.include_router(github_review_router)
app.add_middleware(FeatureFlagMiddleware, client=_flag_client)
_WEBHOOK_TOKEN = settings.webhook_auth_token
_DASHBOARD_PASSWORD = settings.dashboard_password
_ALLOW_INSECURE = insecure_mode_enabled(settings.gateway_allow_insecure)
app.add_middleware(
    AuthMiddleware,
    token=_WEBHOOK_TOKEN,
    dashboard_password=_DASHBOARD_PASSWORD,
    feishu_verification_token=settings.feishu_verification_token,
    allow_insecure=_ALLOW_INSECURE,
)
_MISSING_SECRETS = [
    name
    for name, value in (("WEBHOOK_AUTH_TOKEN", _WEBHOOK_TOKEN), ("DASHBOARD_PASSWORD", _DASHBOARD_PASSWORD))
    if not value
]
if _MISSING_SECRETS:
    if _ALLOW_INSECURE:
        logger.warning(
            "GATEWAY_ALLOW_INSECURE 已开启且 %s 未配置：对应端点将无鉴权放行，禁止用于生产环境",
            ", ".join(_MISSING_SECRETS),
        )
    else:
        logger.warning(
            "%s 未配置：对应受保护端点将统一返回 401（fail-closed）；仅本地调试可设 GATEWAY_ALLOW_INSECURE=1",
            ", ".join(_MISSING_SECRETS),
        )

@app.exception_handler(Exception)
async def _debug_exception_handler(request: Request, exc: Exception):
    import traceback
    if isinstance(exc, MoaError):
        # 业务错误携带自己的码与状态，不再淹没在 500 里
        return JSONResponse(
            status_code=http_status_for(exc.code),
            content={"error": exc.code.value, "message": exc.message},
        )
    if isinstance(exc, InvalidStateTransitionException):
        logger.error("invalid state transition: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": ErrorCode.INVALID_STATE_TRANSITION.value,
                "message": "内部状态迁移错误",
            },
        )
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error("unhandled exception: %s", "".join(tb))
    return JSONResponse(
        status_code=500,
        content={"error": ErrorCode.INTERNAL_ERROR.value, "detail": "内部服务错误", "message": "内部服务错误"},
    )


@app.exception_handler(json.JSONDecodeError)
async def _json_decode_exception_handler(request: Request, exc: json.JSONDecodeError):
    logger.warning("invalid json body: %s", exc)
    return JSONResponse(status_code=400, content={"error": ErrorCode.INVALID_JSON.value, "message": "请求体不是合法 JSON"})


@app.exception_handler(UnicodeDecodeError)
async def _unicode_decode_exception_handler(request: Request, exc: UnicodeDecodeError):
    # 非 UTF-8 字节打到 request.json() 抛的是 UnicodeDecodeError（它不是
    # JSONDecodeError 的子类，父类是 ValueError）——此前裸 500。本质是"请求体
    # 编码不对"，400 才对（探索性验收 D5，2026-09-29）。
    logger.warning("invalid body encoding: %s", exc)
    return JSONResponse(status_code=400, content={"error": ErrorCode.INVALID_JSON.value, "message": "请求体不是合法 UTF-8 编码"})

