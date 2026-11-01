"""交付事件 webhook（issue #26）：GitHub PR 生命周期 → 合并通道。

此前合并通道只有一条入口：delivery / review 管线自己注册自己的 PR。人手（或
其他 agent 会话）开的 PR 对通道**不可见**——CI 绿了没人弹卡，合并永远要人开
GitHub 网页。本路由把 ``pull_request`` 生命周期事件接进**既有** merge 状态机，
其余全部复用，不做第二套实现：

- ``opened`` / ``reopened`` → ``MergeRecord(WATCHING)`` 落 store，飞书发通知卡
  （``notification``，无按钮——此刻没有可批准的东西）；
- ``synchronize`` → ``supersede_older`` 把旧 sha 的记录置 ``SUPERSEDED``（身份
  三元组的设计本来就写着"sha 变了就是另一条记录"），新 sha 落 ``WATCHING``；
- ``closed`` → 打开的记录走 ``FAIL`` 终态，``failure_reason`` 如实记录
  "通道外已合并/已关闭"。（状态机没有"通道外完成"这个动作——加一个就是动
  治理状态机，那要自己的 ADR 和测试，不在本切片里。``FAIL`` 的语义是"终态、
  原因如实告诉人、要不要重来由人决定"，是现有动作里唯一不撒谎的。）

CI 轮询、绿了弹审批卡、批准合并、卡片作废，全部由已有的 MergePoller /
MergeNotifier / MergeExecutor 承担；worker 照常驱动 poller。

安全姿态与 ``/webhook/github/review`` 完全一致：

- ``DELIVERY_WEBHOOK_ENABLED`` 默认关闭（默认行为改变必须显式开）；
- 签名 fail-closed（``verify_signature`` 校验**原始字节**），secret 未配置时仅
  ``GATEWAY_ALLOW_INSECURE=1`` 放行。

仓库范围**不做**白名单过滤：持有 webhook secret 本身就是信任边界，而本项目
的目标使用面是所有者名下的多个仓库（ADR-033 dogfood 的跨仓库假设）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.deps import logger, merge_store
from app.middleware.auth import insecure_mode_enabled
from apps.code_review_pipeline.merge_state import MergeAction, initial_transition
from apps.code_review_pipeline.merge_store import MergeRecord
from apps.code_review_pipeline.notifications.merge_notifier import MergeNotifier
from apps.code_review_pipeline.routing.github_signature import verify_signature

router = APIRouter()

# 只翻译这四个 action，其余（labeled / edited / assigned …）显式忽略。
_HANDLED_ACTIONS = frozenset({"opened", "reopened", "synchronize", "closed"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _notifier() -> MergeNotifier:
    """与 ``webhook._handle_merge_approval`` 同构：请求内构造，不复用全局单例。"""
    return MergeNotifier(default_target=settings.code_review_merge_channel or None)


def _open_records_for(repo: str, pr_number: int) -> list[MergeRecord]:
    return [
        r
        for r in merge_store.list_open()
        if r.repo == repo and r.pr_number == pr_number
    ]


@router.post("/webhook/github/delivery")
async def github_delivery_webhook(request: Request) -> JSONResponse:
    if not settings.delivery_webhook_enabled:
        return JSONResponse({"error": "not_found"}, status_code=404)

    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    ok, reason = verify_signature(
        raw,
        signature,
        settings.github_webhook_secret,
        allow_insecure=insecure_mode_enabled(settings.gateway_allow_insecure),
    )
    if not ok:
        logger.warning("delivery webhook rejected: %s", reason)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    action = str(body.get("action", ""))
    if action not in _HANDLED_ACTIONS:
        return JSONResponse({"status": "ignored"}, status_code=200)

    repo = str((body.get("repository") or {}).get("full_name", ""))
    pr = body.get("pull_request") or {}
    pr_number = int(body.get("number") or pr.get("number") or 0)
    head_sha = str((pr.get("head") or {}).get("sha", ""))
    if not repo or not pr_number or not head_sha:
        logger.warning(
            "delivery webhook payload missing identity fields: action=%s", action
        )
        return JSONResponse(
            {"error": "invalid_payload", "message": "repo/pr_number/head_sha required"},
            status_code=400,
        )

    if action == "opened" or action == "reopened":
        return await _on_opened(repo, pr_number, head_sha, pr)
    if action == "synchronize":
        return await _on_synchronize(repo, pr_number, head_sha)
    return _on_closed(repo, pr_number, pr)


async def _on_opened(
    repo: str, pr_number: int, head_sha: str, pr: dict[str, Any]
) -> JSONResponse:
    identity = (repo, pr_number, head_sha)
    if merge_store.get(identity) is not None:
        return JSONResponse(
            {"status": "idempotent", "state": "watching", "task_key": f"{repo}#{pr_number}@{head_sha}"},
            status_code=200,
        )
    record = MergeRecord(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        transitions=tuple(initial_transition(_now_iso())),
    )
    merge_store.save(record)
    sent = await _notifier().send_pr_opened_card(
        record,
        title=str(pr.get("title", "")),
        url=str(pr.get("html_url", "")),
    )
    logger.info(
        "delivery webhook: watching %s#%s@%s (card_sent=%s)",
        repo,
        pr_number,
        head_sha[:12],
        sent,
    )
    return JSONResponse({"status": "accepted", "state": "watching"}, status_code=202)


async def _on_synchronize(repo: str, pr_number: int, head_sha: str) -> JSONResponse:
    superseded = merge_store.supersede_older(repo, pr_number, keep_sha=head_sha, at=_now_iso())
    record = MergeRecord(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        transitions=tuple(initial_transition(_now_iso())),
    )
    merge_store.save(record)
    logger.info(
        "delivery webhook: new head %s#%s@%s (superseded=%d)",
        repo,
        pr_number,
        head_sha[:12],
        len(superseded),
    )
    return JSONResponse(
        {
            "status": "accepted",
            "state": "watching",
            "superseded": len(superseded),
        },
        status_code=202,
    )


def _on_closed(repo: str, pr_number: int, pr: dict[str, Any]) -> JSONResponse:
    merged = bool(pr.get("merged", False))
    closed = _open_records_for(repo, pr_number)
    for record in closed:
        merge_store.transition(
            record.identity,
            MergeAction.FAIL,
            at=_now_iso(),
            failure_reason=(
                "PR 已在 GitHub 侧合并，未经本通道审批" if merged
                else "PR 已在 GitHub 侧关闭，未经本通道审批"
            ),
        )
    logger.info(
        "delivery webhook: closed %s#%s merged=%s terminated=%d",
        repo,
        pr_number,
        merged,
        len(closed),
    )
    return JSONResponse({"status": "accepted", "terminated": len(closed)})
