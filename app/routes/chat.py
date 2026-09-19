from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.deps import memory, pipeline
from app.fsm.state_machine import Event as FsmEvent
from app.models.errors import ErrorCode
from app.models.events import MoAEvent, new_trace_id

logger = logging.getLogger("moa.routes.chat")
router = APIRouter()

_WEB_SESSION_PREFIX = "web:"


def web_session_id(raw: str) -> str:
    """规范 Web 聊天会话标识：统一加前缀避免与飞书会话混淆。"""
    sid = (raw or "").strip()
    if not sid:
        return ""
    return sid if sid.startswith(_WEB_SESSION_PREFIX) else f"{_WEB_SESSION_PREFIX}{sid}"


class ChatRequest(BaseModel):
    session_id: str = ""
    text: str = ""
    user_id: str = ""


class ChatHistoryRequest(BaseModel):
    session_id: str = ""


@router.post("/dashboard/api/chat")
async def dashboard_chat(body: ChatRequest, request: Request) -> JSONResponse:
    text = body.text.strip()
    if not text:
        return JSONResponse({"error": ErrorCode.VALIDATION_ERROR.value, "message": "text required"}, status_code=400)
    sid = web_session_id(body.session_id)
    if not sid:
        return JSONResponse({"error": ErrorCode.VALIDATION_ERROR.value, "message": "session_id required"}, status_code=400)

    moa_event = MoAEvent(
        trace_id=new_trace_id(),
        event=FsmEvent.MESSAGE_RECEIVED,
        session_id=sid,
        text=text,
        context={"source": "web_chat"},
        user_id=body.user_id.strip(),
    )
    try:
        result = await pipeline.run(
            moa_event, channel="web", target=sid, request=request,
        )
    except Exception:
        logger.exception("web chat pipeline failed session=%s", sid)
        return JSONResponse(
            {"error": "internal_error", "message": "处理消息时出错了"},
            status_code=500,
        )

    # 工具调用痕迹由 TaskAgent 内嵌在汇总文本中（Mock 的 summarize 自带步骤），
    # 此处透传 status/intent 供前端区分命令 / 重置 / 挂起等分支。
    # status=error 不再伪装成 200（旧实现前端只能显示"（空回复）"）：
    # 结构化 500 携带错误码与消息，chat.js 对非 2xx 有现成的降级展示。
    if result.status == "error":
        return JSONResponse({
            "error": result.error_code or ErrorCode.AGENT_FAILED.value,
            "message": result.text or "处理消息时出错了",
            "session_id": sid,
            "trace_id": result.trace_id,
            "status": result.status,
        }, status_code=500)
    return JSONResponse({
        "session_id": sid,
        "trace_id": result.trace_id,
        "text": result.text,
        "status": result.status,
        "intent": result.intent,
    })


@router.post("/dashboard/api/chat/history")
async def dashboard_chat_history(body: ChatHistoryRequest) -> JSONResponse:
    sid = web_session_id(body.session_id)
    if not sid:
        return JSONResponse({"error": ErrorCode.VALIDATION_ERROR.value, "message": "session_id required"}, status_code=400)
    history = memory.get_history(sid, limit=50)
    return JSONResponse({
        "session_id": sid,
        "history": history,
    })


__all__ = ["router", "web_session_id"]
