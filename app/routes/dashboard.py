"""dashboard 路由：JSON API + HTML 页面入口（M3 拆分后的瘦路由层）。

HTML 模板在 ``app/rendering/dashboard_html.py``，请求模型在
``app/schemas/dashboard.py``，审计数据聚合在 ``app/services/audit_stats.py``。
本文件的硬契约：全部 URL/方法、JSON 响应形状、路由注册顺序（带参路径
``/dashboard/sessions/{id}``、``/dashboard/knowledge/{id}`` 必须在固定页之后、
``/dashboard/chat`` 与其余详情页一样直调 ``render_shell``）。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from app.agents.provider import LLMClient, LLMConfig
from app.command_mode import MODES
from app.config import settings
from app.deps import _flag_client, _retriever, command_mode, knowledge_base, memory, obsidian_sync
from app.feature_flags import DEFAULT_FLAGS
from app.rendering.dashboard_html import (
    chat_page,
    knowledge_detail,
    render_page,
    render_shell,
    session_detail,
)
from app.schemas.dashboard import (
    FlagUpdate,
    ModeUpdate,
    OpsConfigUpdate,
    OpsTestRequest,
    SearchQuery,
    UploadDoc,
)
from app.services.audit_stats import (
    hitl_latency_stats,
    load_audit_entries,
    read_recent_logs,
    top_risky_sessions,
    trend_by_day,
)

# 拆分前这批数据函数定义在本模块，test_security_stats.py 直接从这里 import；
# re-export 保持既有测试与外部引用的契约。
_hitl_latency_stats = hitl_latency_stats
_load_audit_entries = load_audit_entries
_top_risky_sessions = top_risky_sessions
_trend_by_day = trend_by_day

__all__ = [
    "router",
    "_hitl_latency_stats",
    "_load_audit_entries",
    "_top_risky_sessions",
    "_trend_by_day",
]

router = APIRouter()


@router.post("/dashboard/upload")
async def dashboard_upload(req: UploadDoc) -> JSONResponse:
    doc_id = await knowledge_base.add_document(req.title, req.content)
    return JSONResponse({"id": doc_id, "status": "ok"})


@router.post("/dashboard/delete")
async def dashboard_delete(body: dict) -> JSONResponse:
    doc_id = body.get("doc_id", "")
    ok = await knowledge_base.delete_doc(doc_id)
    return JSONResponse({"ok": ok})


@router.post("/dashboard/api/knowledge/upload_file")
async def dashboard_upload_file(
    file: UploadFile = File(...),
    title: str = Form(""),
) -> JSONResponse:
    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="replace")
    if not content.strip():
        return JSONResponse({"error": "empty file"}, status_code=400)
    name = (title or file.filename or "document").strip()
    doc_id = await knowledge_base.add_document(name, content)
    return JSONResponse({"id": doc_id, "title": name, "status": "ok"})


@router.get("/dashboard/api/knowledge/{doc_id}")
async def dashboard_knowledge_detail(doc_id: str) -> JSONResponse:
    doc = await knowledge_base.get_doc(doc_id)
    if doc is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse({"doc": doc})


@router.post("/dashboard/api/knowledge/search")
async def dashboard_knowledge_search(body: SearchQuery) -> JSONResponse:
    if not body.query.strip():
        return JSONResponse({"error": "query required"}, status_code=400)
    result = await _retriever.retrieve(body.query)
    hits = [{"chunk": chunk, "index": i} for i, chunk in enumerate(result.chunks, start=1)]
    return JSONResponse({"hits": hits, "doc_count": result.doc_count})


@router.get("/dashboard/api/sessions")
async def dashboard_sessions() -> JSONResponse:
    sessions = []
    seen: set[str] = set()
    for sid in memory.list_sessions():
        seen.add(sid)
        full = memory.get_history(sid, limit=50)
        history = full[-6:]
        mode = command_mode.get(sid) or "default"
        sessions.append({
            "id": sid,
            "session_id": sid[:16],
            "mode": mode,
            "mode_label": MODES.get(mode, {}).get("label", mode),
            "message_count": len(full),
            "history": [{"role": h["role"], "content": h["content"][:80]} for h in history],
        })
    for sid in list(command_mode._store.keys()):
        if sid not in seen:
            mode = command_mode.get(sid) or "default"
            sessions.append({
                "id": sid,
                "session_id": sid[:16],
                "mode": mode,
                "mode_label": MODES.get(mode, {}).get("label", mode),
                "message_count": 0,
                "history": [],
            })
    return JSONResponse({"sessions": sessions})


@router.get("/dashboard/api/sessions/{session_id}")
async def dashboard_session_detail(session_id: str) -> JSONResponse:
    mode = command_mode.get(session_id) or "default"
    history = memory.get_history(session_id, limit=50)
    return JSONResponse({
        "id": session_id,
        "mode": mode,
        "mode_label": MODES.get(mode, {}).get("label", mode),
        "history": [{"role": h["role"], "content": h["content"]} for h in history],
    })


@router.post("/dashboard/api/sessions/{session_id}/mode")
async def dashboard_session_mode(session_id: str, body: ModeUpdate) -> JSONResponse:
    mode = body.mode.strip().lower()
    if mode not in MODES:
        return JSONResponse({"ok": False, "error": f"unknown mode: {mode}"}, status_code=400)
    command_mode.set(session_id, mode)
    return JSONResponse({"ok": True, "mode": mode, "mode_label": MODES[mode]["label"]})


@router.post("/dashboard/api/sessions/{session_id}/clear")
async def dashboard_clear_session(session_id: str) -> JSONResponse:
    memory.clear(session_id)
    command_mode.clear(session_id)
    return JSONResponse({"ok": True, "session_id": session_id})


@router.get("/dashboard/api/logs")
async def dashboard_logs() -> JSONResponse:
    return JSONResponse({"logs": read_recent_logs(100)})


@router.get("/dashboard/api/ops/config")
async def dashboard_ops_config() -> JSONResponse:
    flag_names = set(DEFAULT_FLAGS.keys())
    for key in _flag_client._store.keys():
        flag_names.add(key.removeprefix("moa:flag:"))
    flags = []
    for name in sorted(flag_names):
        value = await _flag_client.get(name, DEFAULT_FLAGS.get(name, False))
        flags.append({"name": name, "value": value})
    return JSONResponse({
        "llm": {
            "provider": os.environ.get("LLM_PROVIDER", "direct"),
            "model": os.environ.get("LLM_MODEL", ""),
            "base_url": os.environ.get("LLM_BASE_URL", ""),
            "api_key_set": bool(os.environ.get("LLM_API_KEY", "")),
        },
        "feishu": {"configured": bool(os.environ.get("FEISHU_APP_ID", ""))},
        "redis": {"url": settings.redis_url},
        "tracing": {"otlp_endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")},
        "limiter": {"wired": False, "note": "未接入全局限流"},
        "obsidian": obsidian_sync.status(),
        "flags": flags,
    })


@router.post("/dashboard/api/ops/config")
async def dashboard_ops_config_update(body: OpsConfigUpdate) -> JSONResponse:
    if body.provider is not None:
        os.environ["LLM_PROVIDER"] = body.provider.strip().lower() or "direct"
    if body.model is not None:
        os.environ["LLM_MODEL"] = body.model.strip()
    if body.base_url is not None:
        os.environ["LLM_BASE_URL"] = body.base_url.strip().rstrip("/")
    if body.api_key:
        os.environ["LLM_API_KEY"] = body.api_key.strip()
    return JSONResponse({
        "ok": True,
        "llm": {
            "provider": os.environ.get("LLM_PROVIDER", "direct"),
            "model": os.environ.get("LLM_MODEL", ""),
            "base_url": os.environ.get("LLM_BASE_URL", ""),
            "api_key_set": bool(os.environ.get("LLM_API_KEY", "")),
        },
    })


@router.post("/dashboard/api/ops/test")
async def dashboard_ops_test(body: OpsTestRequest) -> JSONResponse:
    config = LLMConfig.from_env("LLM")
    if body.provider is not None:
        config.provider = body.provider.strip().lower() or "direct"
    if body.model:
        config.model = body.model.strip()
    if body.base_url:
        config.base_url = body.base_url.strip().rstrip("/")
    if body.api_key:
        config.api_key = body.api_key.strip()
    config.timeout = 30.0
    config.max_tokens = 64
    client = LLMClient(config)
    try:
        reply = await client.chat(
            [{"role": "user", "content": (body.message or "ping")[:500]}],
            max_tokens=64,
        )
    except Exception as exc:
        # 连接失败是本探测端点的"正常结果"而非服务器错误：保持 200 +
        # ok:false，前端按"连接失败"渲染（dashboard.js 依赖该形状）。
        return JSONResponse({"ok": False, "error": str(exc)[:500]})
    finally:
        await client.aclose()
    return JSONResponse({"ok": True, "reply": reply[:2000]})


@router.post("/dashboard/api/ops/flags/{name}")
async def dashboard_flag_set(name: str, body: FlagUpdate) -> JSONResponse:
    try:
        _flag_client.validate_value(name, body.value)
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            {"ok": False, "error": "invalid_flag_value", "detail": str(exc)},
            status_code=400,
        )
    await _flag_client.set(name, body.value)
    value = await _flag_client.get(name)
    return JSONResponse({"ok": True, "name": name, "value": value})


@router.delete("/dashboard/api/ops/flags/{name}")
async def dashboard_flag_delete(name: str) -> JSONResponse:
    await _flag_client.delete(name)
    return JSONResponse({"ok": True, "name": name})


@router.post("/dashboard/api/ops/obsidian/sync")
async def dashboard_obsidian_sync() -> JSONResponse:
    if not obsidian_sync.enabled:
        return JSONResponse({
            "ok": True,
            "changed": 0,
            "message": "Obsidian 未启用，未执行同步",
            **obsidian_sync.status(),
        })
    changed = await obsidian_sync.sync_once()
    return JSONResponse({"ok": True, "changed": changed, **obsidian_sync.status()})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_overview() -> HTMLResponse:
    return render_page("overview")


@router.get("/dashboard/overview", response_class=HTMLResponse)
async def dashboard_overview_page() -> HTMLResponse:
    return render_page("overview")


@router.get("/dashboard/knowledge", response_class=HTMLResponse)
async def dashboard_knowledge_page() -> HTMLResponse:
    return render_page("knowledge")


@router.get("/dashboard/sessions", response_class=HTMLResponse)
async def dashboard_sessions_page() -> HTMLResponse:
    return render_page("sessions")


@router.get("/dashboard/test", response_class=HTMLResponse)
async def dashboard_test_page() -> HTMLResponse:
    return render_page("test")


@router.get("/dashboard/logs", response_class=HTMLResponse)
async def dashboard_logs_page() -> HTMLResponse:
    return render_page("logs")


@router.get("/dashboard/security", response_class=HTMLResponse)
async def dashboard_security_page() -> HTMLResponse:
    return render_page("security")


@router.get("/dashboard/ops", response_class=HTMLResponse)
async def dashboard_ops_page() -> HTMLResponse:
    return render_page("ops")


@router.get("/dashboard/sessions/{session_id}", response_class=HTMLResponse)
async def dashboard_session_detail_page(session_id: str) -> HTMLResponse:
    return render_shell(
        "会话详情",
        "session-detail",
        "完整对话记录与模式管理",
        session_detail(session_id, command_mode.get(session_id)),
        active_key="sessions",
    )


@router.get("/dashboard/knowledge/{doc_id}", response_class=HTMLResponse)
async def dashboard_knowledge_detail_page(doc_id: str) -> HTMLResponse:
    return render_shell(
        "文档详情",
        "knowledge-detail",
        "文档内容与分块详情",
        knowledge_detail(doc_id),
        active_key="knowledge",
    )

@router.get("/dashboard/chat", response_class=HTMLResponse)
async def dashboard_chat_page() -> HTMLResponse:
    return render_shell(
        "对话",
        "chat",
        "自主任务 Agent 对话界面",
        chat_page(),
    )
