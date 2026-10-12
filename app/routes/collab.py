"""多 Agent 协作的显式入口（ADR-022 / R6）。

为什么是一个独立端点而不是接进 ``/dashboard/api/chat``
----------------------------------------------------
协作链路**不在请求主路径上**：``INTENT_AGENT_MAP`` / 意图路由不认识它，
``MoAPipeline`` 也不知道它。这不是没做完，是 ADR-022 定下的边界——把多 Agent
协作塞进主路径会让每一次请求都付出 supervisor + critic 的额外延迟与 token，
而绝大多数请求不需要它。所以它是**显式**选择的能力：知道自己在做什么的人
调 ``POST /api/v1/collab``。

鉴权是白名单制（见 ``app/middleware/auth.py`` 的 ``_DASH_PROTECTED_PREFIXES``）：
任何不在 ``_ALLOWED`` 里的路径都要鉴权，``/api/`` 前缀天然落在受保护侧，
所以这里不需要也不应该再写一遍。

审批回路
--------
协作的挂起活在 LangGraph checkpoint 里（它刻意不驱动会话 FSM，理由见
``app/orchestration/collaboration.py`` 的类 docstring）。所以放行也走这里：
``GET /api/v1/collab/pending/{trace_id}`` 看负载，
``POST /api/v1/collab/approve`` 用 ``Command(resume=...)`` 放行或驳回。
飞书卡片回调那条回路**不**服务协作——它驱动 FSM，对协作会话只会得到
"审批已失效"，那会是一个撒谎的答案。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.models.errors import ErrorCode

logger = logging.getLogger("moa.routes.collab")
router = APIRouter()

_orchestrator = None


def _collab():
    """懒构造协作编排器。

    懒的原因很实在：``CollabOrchestrator.from_deps()`` 要 import
    ``app.orchestration.collaboration``，而 langgraph 是**可选 extra**
    （Docker 镜像 ``uv sync`` 不带它）。在模块 import 时就构造，等于让整个
    网关在没有 langgraph 的环境里启动失败——而协作只是新增能力，不该有这个权力。
    """
    global _orchestrator
    if _orchestrator is None:
        try:
            from app.orchestration.collaboration import CollabOrchestrator
        except Exception as exc:  # noqa: BLE001 - 缺 extra 时端点返回 503，不影响启动
            logger.warning("langgraph 不可用，协作链路无法启动: %s", exc)
            return None
        # 构造失败的处置与 import 失败不同：langgraph 已经装上，报 503 等于把
        # 真实故障伪装成"能力不存在"，所以让它抛出去按 500 走统一错误契约。
        _orchestrator = CollabOrchestrator.from_deps()
    return _orchestrator


class CollabRunRequest(BaseModel):
    task: str = ""
    session_id: str = ""
    user_id: str = ""
    channel: str = ""
    target: str = ""


class CollabApproveRequest(BaseModel):
    trace_id: str = ""
    decision: str = "approve"


def _unavailable() -> JSONResponse:
    return JSONResponse(
        {
            "error": ErrorCode.VALIDATION_ERROR.value,
            "message": (
                "协作链路不可用：langgraph 未安装，或 COLLAB_ENABLED=0。"
                "详见 docs/multi-agent-collaboration.md"
            ),
        },
        status_code=503,
    )


@router.post("/api/v1/collab")
async def run_collab(body: CollabRunRequest, request: Request) -> JSONResponse:
    if not settings.collab_enabled:
        return _unavailable()
    orchestrator = _collab()
    if orchestrator is None:
        return _unavailable()

    task = body.task.strip()
    if not task:
        return JSONResponse(
            {"error": ErrorCode.VALIDATION_ERROR.value, "message": "task required"},
            status_code=400,
        )

    try:
        result = await orchestrator.run(
            task=task,
            session_id=body.session_id.strip(),
            channel=body.channel.strip(),
            target=body.target.strip(),
            user_id=body.user_id.strip(),
        )
    except Exception:
        logger.exception("collab run failed task=%s", task[:80])
        return JSONResponse(
            {"error": ErrorCode.INTERNAL_ERROR.value, "message": "协作执行失败"},
            status_code=500,
        )
    return JSONResponse({
        "trace_id": result.trace_id,
        "status": result.status,
        "text": result.final_text,
        "need_human_review": result.need_human_review,
        "guard_action": result.guard_action,
        "error_code": result.error_code,
        "rounds": result.rounds,
        "plan": [item.to_dict() for item in result.plan],
        "per_agent_outputs": result.per_agent_outputs,
        "critique_history": [item.to_dict() for item in result.critique_history],
        "cost_usd": result.cost_usd,
        "llm_latency_ms": result.llm_latency_ms,
        "tool_calls": result.tool_calls,
        "node_path": list(result.node_path),
    })


@router.get("/api/v1/collab/pending/{trace_id}")
async def collab_pending(trace_id: str) -> JSONResponse:
    if not settings.collab_enabled:
        return _unavailable()
    orchestrator = _collab()
    if orchestrator is None:
        return _unavailable()
    payload = orchestrator.pending_payload(trace_id)
    if payload is None:
        return JSONResponse(
            {"error": ErrorCode.HITL_REQUEST_NOT_FOUND.value, "message": "没有挂起中的协作"},
            status_code=404,
        )
    return JSONResponse({"trace_id": trace_id, "pending": payload})


@router.post("/api/v1/collab/approve")
async def collab_approve(body: CollabApproveRequest) -> JSONResponse:
    if not settings.collab_enabled:
        return _unavailable()
    orchestrator = _collab()
    if orchestrator is None:
        return _unavailable()
    trace_id = body.trace_id.strip()
    if not trace_id:
        return JSONResponse(
            {"error": ErrorCode.VALIDATION_ERROR.value, "message": "trace_id required"},
            status_code=400,
        )
    decision = (
        "approve"
        if body.decision.strip().lower() in ("approve", "approved", "ok", "true")
        else "reject"
    )
    try:
        result = await orchestrator.resume(trace_id, decision)
    except Exception:
        logger.exception("collab resume failed trace=%s", trace_id)
        return JSONResponse(
            {"error": ErrorCode.INTERNAL_ERROR.value, "message": "协作续跑失败"},
            status_code=500,
        )
    return JSONResponse({
        "trace_id": result.trace_id,
        "status": result.status,
        "text": result.final_text,
        "need_human_review": result.need_human_review,
        "node_path": list(result.node_path),
    })


__all__ = ["router"]
