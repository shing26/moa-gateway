from __future__ import annotations
import logging, os, pathlib
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry import trace
import app.agents.loader
from app.deps import (
    _card_sender, _feishu_config, _flag_client, engine, logger,
    init_feishu, init_prompts, obsidian_sync, tracer,
)
from app.observability.tracing import setup_tracing, TraceConfig
from app.middleware.flags import FeatureFlagMiddleware
from app.routes.dashboard import router as dashboard_router
from app.routes.feishu import router as feishu_router
from app.routes.health import router as health_router
from app.routes.webhook import webhook_router
from app.routes.knowledge import router as knowledge_router

app = FastAPI(title="MoA Engine Gateway", version="0.1.0")
STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
app.mount("/dashboard/static", StaticFiles(directory=STATIC_DIR), name="dashboard-static")
app.include_router(dashboard_router)
app.include_router(feishu_router)
app.include_router(health_router)
app.include_router(webhook_router)
app.include_router(knowledge_router)
app.add_middleware(FeatureFlagMiddleware, client=_flag_client)

@app.exception_handler(Exception)
async def _debug_exception_handler(request: Request, exc: Exception):
    import traceback
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error("unhandled exception: %s", "".join(tb))
    return JSONResponse(status_code=500, content={"error": type(exc).__name__, "detail": str(exc)[:500]})

@app.on_event("startup")
async def _startup() -> None:
    global tracer
    try:
        cfg = TraceConfig(otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
        setup_tracing(cfg)
    except Exception:
        logger.warning("opentelemetry tracing init failed")
    init_feishu()
    init_prompts()
    await obsidian_sync.start()
    tracer = trace.get_tracer("moa-gateway")

@app.on_event("shutdown")
async def _shutdown() -> None:
    logger.info("moa gateway shutting down")
    await obsidian_sync.close()
    engine.session_store.clear_all()
    _flag_client.invalidate()
