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
from app.observability.tracing import setup_tracing, TraceConfig
from app.middleware.auth import AuthMiddleware, insecure_mode_enabled
from app.middleware.flags import FeatureFlagMiddleware
from app.config import settings
from app.routes.dashboard import router as dashboard_router
from app.routes.feishu import router as feishu_router
from app.routes.health import router as health_router
from app.routes.webhook import webhook_router
from app.routes.knowledge import router as knowledge_router
from apps.code_review_pipeline.routing.github_review_route import github_review_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    global tracer
    try:
        cfg = TraceConfig(otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
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
app.include_router(feishu_router)
app.include_router(health_router)
app.include_router(webhook_router)
app.include_router(knowledge_router)
app.include_router(github_review_router)
app.add_middleware(FeatureFlagMiddleware, client=_flag_client)
_WEBHOOK_TOKEN = os.environ.get("WEBHOOK_AUTH_TOKEN", "")
_DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
_ALLOW_INSECURE = insecure_mode_enabled(os.environ.get("GATEWAY_ALLOW_INSECURE"))
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
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error("unhandled exception: %s", "".join(tb))
    return JSONResponse(status_code=500, content={"error": "internal_error", "detail": "内部服务错误"})


@app.exception_handler(json.JSONDecodeError)
async def _json_decode_exception_handler(request: Request, exc: json.JSONDecodeError):
    logger.warning("invalid json body: %s", exc)
    return JSONResponse(status_code=400, content={"error": "invalid_json", "detail": "请求体不是合法 JSON"})

