from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.channels.base import ChannelMessage
from app.channels.feishu import FeishuChannelAdapter, FeishuConfig
from app.channels.feishu_auth import FeishuAuthConfig, FeishuTokenProvider
from app.channels.feishu_event import parse_feishu_event
from app.channels.feishu_signature import verify_verification_token
from app.config import settings
from app.deps import (
    adapter,
    delivery_run_store,
    engine,
    merge_store,
    pipeline,
)
from app.middleware.auth import approver_gate_error, insecure_mode_enabled
from app.middleware.request_logger import bind_trace, log_request
from app.models.errors import ErrorCode
from app.fsm.state_machine import Event as FsmEvent
from app.models.events import MoAEvent, new_trace_id
from app.pipeline import PipelineResult
from apps.code_review_pipeline.delivery.audit import WalDeliveryAudit
from apps.code_review_pipeline.delivery.feishu_approval import (
    PUSH_APPROVAL_KIND,
    shared_storage_available,
    write_decision,
)
from apps.code_review_pipeline.merge_executor import MergeExecutor
from apps.code_review_pipeline.notifications.merge_notifier import MergeNotifier
from apps.code_review_pipeline.routing.github_provider import build_github_client


router = APIRouter()
_adapter = None
_seen_events = set()
_MAX_SEEN = 100
# fire-and-forget 的 HITL 闭环任务必须持引用：asyncio 只保留弱引用，
# 请求作用域结束后任务可能被 GC 回收、中途静默消失（官方文档明确警告）。
_background_tasks: set[asyncio.Task] = set()
_review_store = None
# Review-card callbacks are low volume. Serialize them inside the gateway so a
# double-click cannot write two conflicting human-decision audits before the
# database state transition rejects the loser.
_review_action_lock = asyncio.Lock()

logger = logging.getLogger("moa.routes.feishu")


def _get_review_store():
    global _review_store
    if _review_store is None:
        from apps.code_review_pipeline.storage.review_store import build_review_store

        _review_store = build_review_store()
    return _review_store


def _review_task_id(trace_id: str) -> str:
    return trace_id.removeprefix("cr_")


def _review_finding_dicts(findings: tuple[object, ...]) -> tuple[dict, ...]:
    return tuple(
        {
            "finding_uid": item.finding_uid,
            "severity": item.severity,
            "file": item.file,
            "line": item.line,
            "title": item.title,
            "description": item.description,
            "suggestion": item.suggestion,
            "evidence": list(item.evidence),
        }
        for item in findings
        if getattr(item, "status", "") == "published"
    )

async def get_adapter():
    global _adapter
    if _adapter is not None:
        return _adapter
    app_id = settings.feishu_app_id
    app_secret = settings.feishu_app_secret
    if app_id and app_secret:
        auth = FeishuTokenProvider(FeishuAuthConfig(app_id=app_id, app_secret=app_secret))
        _adapter = FeishuChannelAdapter(FeishuConfig(app_id=app_id, app_secret=app_secret), auth=auth)
    return _adapter

def _dedup(event_id: str) -> bool:
    global _seen_events
    if event_id in _seen_events:
        return False
    _seen_events.add(event_id)
    if len(_seen_events) > _MAX_SEEN:
        _seen_events.clear()
    return True


async def _safe_send(target: str, text: str, trace_id: str) -> None:
    adp = await get_adapter()
    if not adp:
        logger.error("card_action adapter is None; drop reply to %s", target)
        return
    await adp.send(ChannelMessage(channel="feishu", target=target, text=text, trace_id=trace_id))


async def _process_hitl_card(
    action: str, trace_id: str, session_id: str, operator_id: str = ""
) -> None:
    """卡片按钮点击后的真实 HITL 闭环，与 /webhook/callback 同一套语义：
    认领挂起请求 → FSM 推进 HUMAN_APPROVED / HUMAN_REJECTED → 送达或丢弃
    输出 → 审计留痕（guard_action=hitl_approve / hitl_reject / hitl_expired）。

    **认领用 `pop_hitl`（原子取走并删除）而非"读 → 判断 → 删"**：那张卡片被连点两次、
    或两个回调并发到达时，第二个调用者必须拿不到 payload，否则会重复送达 + 写两条审计。
    """
    hitl_id = trace_id or session_id
    started = time.time()
    # 后台任务在独立上下文中运行：显式绑定，保证决策审计与触发审批的
    # 请求共享同一 trace（人工决策因此可按 trace 回流评测）
    bind_trace(trace_id or session_id)
    if not trace_id:
        # 没带 trace_id 时 bind_trace 退化成拿 session_id 当 trace：决策本身仍能按会话
        # 查到，但它**join 不回**触发它的那条拦截（collect_hitl_feedback 按 trace 对齐）。
        # 留一条 warning，让"这次审批进不了评测"是可见的，而不是无声的。
        logger.warning(
            "card_action 未携带 trace_id，本次决策无法按 trace 回流评测（hitl_id=%s）", hitl_id
        )
    duration_ms = 0.0
    outcome = "hitl_not_found"
    try:
        # 审批人闸门（`HITL_APPROVER_IDS`）。**必须在认领之前判**：不能让没有权限的人
        # 把待审批记录点掉——那样别人就再也批不了了。白名单为空时 fail-closed
        # （除非显式 `GATEWAY_ALLOW_INSECURE=1`），见 middleware.auth.approver_gate_error。
        gate_error = approver_gate_error(
            operator_id,
            settings.hitl_approver_ids,
            raw_insecure=settings.gateway_allow_insecure,
        )
        if gate_error is not None:
            logger.warning("card_action rejected: %s (hitl_id=%s)", gate_error, hitl_id)
            await _safe_send(session_id, "你没有审批该请求的权限", trace_id)
            outcome = "hitl_forbidden"
            return
        hitl = engine.session_store.pop_hitl(hitl_id)
        if hitl is None:
            # 不存在 / 已失效 / 已被（并发的另一次点击）认领，三种情况同一处理：
            # 不送达、不写决策审计。
            logger.warning("card_action hitl not available hitl_id=%s", hitl_id)
            await _safe_send(session_id, "该审批请求已失效或已被处理", trace_id)
            return
        # ADR-021 合并审批分叉：merge_approval 不走聊天 FSM，直接执行合并
        if hitl.hitl_kind == "merge_approval":
            # 提前把 outcome 落定，否则 finally 那条 "card_action closed" 会打印
            # 初始值 hitl_not_found——成功合并的日志看起来像"没找到审批"（误导排查）。
            outcome = f"merge_{action}"
            return await _handle_merge_approval(
                hitl_id=hitl_id,
                action=action,
                operator_id=operator_id,
                session_id=session_id,
                trace_id=trace_id,
                created_at=hitl.created_at,
            )
        if hitl.hitl_kind == PUSH_APPROVAL_KIND:
            # A1：回调只记录决定，推送仍由操作者机器上的 CLI 用其 PAT 执行。
            outcome = f"push_approval_{action}"
            return await _handle_push_approval_decision(
                hitl=hitl,
                hitl_id=hitl_id,
                action=action,
                operator_id=operator_id,
                session_id=session_id,
                trace_id=trace_id,
            )
        ctx, expired = await engine.decide_hitl(
            session_id=session_id, trace_id=trace_id, approve=(action == "approve"),
        )
        # 审计字段 `hitl_duration_ms` 的语义必须与 /webhook/callback 一致：那里算的是
        # "从挂起到人工决定"的**真实时延**，而这里此前塞的是**回调处理耗时**（毫秒级）
        # —— 同名两义，且 dashboard 的"人工审批耗时分布"读的正是该字段（2026-09-23 修）。
        # `duration_ms` 仍保留给日志/outcome，表示本次处理耗时。
        human_latency_ms = (
            round((time.time() - hitl.created_at) * 1000, 1) if hitl.created_at > 0 else 0.0
        )
        if expired:
            # 重启后挂起记录还在、FSM 会话状态已丢：告知并结束（记录已被 pop 消耗）。
            duration_ms = round((time.time() - started) * 1000, 1)
            await log_request(
                None, 200, duration_ms, session_id=session_id,
                agent_name=hitl.agent_name, intent=hitl.intent,
                guard_action="hitl_expired", input_text="",
                output_text=hitl.agent_output[:2000], hitl_decision=action,
                hitl_duration_ms=human_latency_ms, hitl_kind=hitl.hitl_kind,
                hitl_operator=operator_id,
            )
            await _safe_send(
                session_id,
                "该审批已失效（审批可能已处理，或服务重启过），请重新发起该请求",
                trace_id,
            )
            outcome = "hitl_expired"
            return
        # 本地格式化在送达之前（纯本地操作，不产生副作用）；而挂起记录已在上面的
        # pop 里认领 —— 若此处格式化或随后发送失败，该记录不会回来，用户需重新发起。
        # 这是"单次决策"的代价：与之相比重复送达是更坏的语义。
        if action == "approve":
            adapted = adapter.adapt(hitl.agent_output, channel=hitl.channel, target=hitl.target)
            body = "✅ 已批准，以下是输出：\n" + (adapted.text or hitl.agent_output)
        else:
            body = "❌ 已拒绝，输出已按你的选择丢弃"
        duration_ms = round((time.time() - started) * 1000, 1)
        await log_request(
            None, 200, duration_ms, session_id=session_id,
            agent_name=hitl.agent_name, intent=hitl.intent,
            guard_action=f"hitl_{action}", input_text="",
            output_text=hitl.agent_output[:2000], hitl_decision=action,
            hitl_duration_ms=human_latency_ms, hitl_kind=hitl.hitl_kind,
            hitl_operator=operator_id,
        )
        await _safe_send(session_id, body, trace_id)
        outcome = f"hitl_{action}"
    except Exception:  # noqa: BLE001 - 卡片动作处理：任意失败记录并回告用户，回调必须闭环
        outcome = "hitl_error"
        logger.exception("card_action hitl processing failed trace=%s action=%s", trace_id, action)
        try:
            await _safe_send(session_id, "处理审批时出错了，请稍后重试", trace_id)
        except Exception:  # noqa: BLE001 - 错误通知失败：任意异常只记录，不能掩盖原始错误
            logger.exception("card_action error notify also failed trace=%s", trace_id)
    finally:
        logger.info(
            "card_action closed trace=%s action=%s outcome=%s duration_ms=%s",
            trace_id, action, outcome, duration_ms,
        )


async def _handle_push_approval_decision(
    *,
    hitl: Any,
    hitl_id: str,
    action: str,
    operator_id: str,
    session_id: str,
    trace_id: str,
) -> None:
    """A1 推送审批回调：校验身份与 bundle 绑定，写 decision，不执行推送。

    白名单在 ``pop_hitl`` 之前已由 ``approver_gate_error`` 判定（无权限的点击
    不消耗记录）。这里只剩下：共享存储、bundle 绑定、自审批三态。
    """
    payload = dict(getattr(hitl, "delivery_payload", None) or {})
    run_id = str(payload.get("run_id", ""))
    pending_hash = str(payload.get("bundle_hash", ""))
    reply_target = getattr(hitl, "target", "") or session_id
    ttl_s = max(60, int(settings.feishu_push_approval_timeout_s) + 60)
    base_gate = "allowlist" if settings.hitl_approver_ids else "insecure_allowlist_empty"
    human_latency_ms = (
        round((time.time() - hitl.created_at) * 1000, 1)
        if getattr(hitl, "created_at", 0.0) > 0
        else 0.0
    )

    def _decision(decision: str, *, reason: str, gate: str) -> dict[str, Any]:
        return {
            "decision": decision,
            "approver": operator_id,
            "channel": "feishu",
            "identity_verified": True,
            "gate": gate,
            "bundle_hash": pending_hash,
            "reason": reason,
        }

    async def _record_and_reply(
        decision: str,
        *,
        reason: str,
        gate: str,
        message: str,
    ) -> None:
        stored = write_decision(
            engine.session_store,
            hitl_id,
            _decision(decision, reason=reason, gate=gate),
            ttl_s=ttl_s,
        )
        await log_request(
            None,
            200,
            0.0,
            session_id=session_id,
            agent_name=hitl.agent_name,
            intent=hitl.intent,
            guard_action=(
                f"push_approval_{reason or decision}"
                if stored
                else "push_approval_write_failed"
            ),
            input_text="",
            output_text=json.dumps(payload, ensure_ascii=False)[:2000],
            hitl_decision=decision if stored else "error",
            hitl_duration_ms=human_latency_ms,
            hitl_kind=hitl.hitl_kind,
            hitl_operator=operator_id,
        )
        if not stored:
            message = "审批决定未能写入共享存储（Redis 未连接），请检查后重试"
        await _safe_send(reply_target, message, trace_id)

    if not shared_storage_available(engine.session_store):
        await _safe_send(
            reply_target,
            "审批存储不可用（Redis 未连接），本次点击未记录",
            trace_id,
        )
        return

    current_hash = ""
    if run_id:
        try:
            run = delivery_run_store.load(run_id)
        except Exception:  # noqa: BLE001 - run 缺失/损坏都按 bundle 不符处理，fail-closed
            logger.exception("push approval run lookup failed run_id=%s", run_id)
            run = None
        if run is not None and getattr(run, "bundle", None) is not None:
            current_hash = str(run.bundle.bundle_hash)
    if not run_id or not pending_hash or current_hash != pending_hash:
        await _record_and_reply(
            "reject",
            reason="bundle_mismatch",
            gate=base_gate,
            message="bundle 已变化或记录不存在，本次审批作废",
        )
        return

    operator_open_id = (settings.feishu_operator_open_id or "").strip()
    insecure = insecure_mode_enabled(settings.gateway_allow_insecure)
    gate = base_gate
    if operator_open_id:
        if operator_id.strip() == operator_open_id:
            if not insecure:
                await _record_and_reply(
                    "reject",
                    reason="self_approval",
                    gate=base_gate,
                    message="不能批准自己发起的推送（self_approval）",
                )
                return
            gate = "insecure_self_approval"
    elif not insecure:
        await _record_and_reply(
            "reject",
            reason="operator_unverified",
            gate=base_gate,
            message="FEISHU_OPERATOR_OPEN_ID 未配置，无法验证 approver ≠ operator",
        )
        return
    else:
        gate = "insecure_operator_unverified"

    if action == "approve":
        await _record_and_reply(
            "approve",
            reason="",
            gate=gate,
            message="✅ 已批准推送；操作者终端将继续执行",
        )
    else:
        await _record_and_reply(
            "reject",
            reason="rejected_by_human",
            gate=gate,
            message="❌ 已拒绝推送",
        )


async def _process_review_card_action(
    *,
    action: str,
    value: dict,
    operator_id: str,
    session_id: str,
    event_id: str,
    message_id: str = "",
) -> None:
    """Handle per-finding feedback and the terminal publish/reject decisions.

    This path is intentionally separate from chat HITL and merge approval. It
    only acts on persisted review findings and never calls the merge API.
    """
    trace_id = str(value.get("trace_id") or "")
    head_sha = str(value.get("head_sha") or "")
    gate_error = approver_gate_error(
        operator_id,
        settings.hitl_approver_ids,
        raw_insecure=settings.gateway_allow_insecure,
    )
    if gate_error is not None:
        logger.warning("review card action rejected: %s trace=%s", gate_error, trace_id)
        await _safe_send(session_id, "你没有审批该请求的权限", trace_id)
        return
    if not trace_id:
        await _safe_send(session_id, "卡片缺少 trace_id，已忽略", trace_id)
        return
    if not settings.code_review_finding_feedback_enabled:
        await _safe_send(session_id, "PR finding 反馈闭环当前已关闭", trace_id)
        return

    store = _get_review_store()
    record = store.get(trace_id)
    if record is None:
        await _safe_send(session_id, "审查记录不存在或已过期", trace_id)
        return
    current_trace = store.current_trace_id(record.repo, record.pr_number)
    if current_trace and current_trace != trace_id:
        await _safe_send(session_id, "该卡片对应的审查版本已过期，请使用最新卡片", trace_id)
        return
    identity = (record.repo, record.pr_number, record.head_sha)
    if head_sha and head_sha != record.head_sha:
        await _safe_send(session_id, "该卡片对应的 head SHA 已过期，请使用最新卡片", trace_id)
        return
    review_run_id = str(value.get("review_run_id") or "")
    if review_run_id != record.review_run_id:
        await _safe_send(session_id, "该卡片对应的审查运行版本已过期，请使用最新卡片", trace_id)
        return
    state = store.get_task_state(identity)
    recorded_review_id = store.get_posted_review_id(identity)
    if action == "review_publish" and recorded_review_id and state != "posting":
        await _safe_send(
            session_id,
            f"该审查已写回 GitHub（already_posted, review={recorded_review_id}）",
            trace_id,
        )
        return
    posting_retry = action == "review_publish" and state == "posting"
    if state not in {"running", "waiting_approval"} and not posting_retry:
        await _safe_send(session_id, f"当前任务状态为 {state or 'unknown'}，不能再执行该操作", trace_id)
        return

    if action == "review_finding_feedback":
        await _record_review_feedback(
            store=store,
            record=record,
            value=value,
            operator_id=operator_id,
            session_id=session_id,
            event_id=event_id,
            message_id=message_id,
        )
        return
    if action not in {"review_publish", "review_reject"}:
        logger.warning("unknown review card action=%s trace=%s", action, trace_id)
        return

    status = store.feedback_status(trace_id)
    if not status.complete and not posting_retry:
        await _safe_send(
            session_id,
            f"还有 {status.total_findings - status.labelled_findings} 条 finding 未标注",
            trace_id,
        )
        return

    # Feedback can span minutes. Recheck immediately before the terminal action;
    # the database transition remains the authoritative final guard.
    current_trace = store.current_trace_id(record.repo, record.pr_number)
    if current_trace and current_trace != trace_id:
        await _safe_send(
            session_id,
            "该卡片对应的审查版本已过期，请使用最新卡片",
            trace_id,
        )
        return

    # Finding pages are delivered while the worker remains ``running`` so a
    # crash before notification is still reclaimable. A terminal action that
    # arrives before the worker's final transition can safely establish the
    # waiting state itself once every finding has been labelled.
    if state == "running":
        try:
            store.transition_task(
                identity,
                "request_approval",
                expected_review_run_id=record.review_run_id,
            )
        except Exception:  # noqa: BLE001 - 幂等转换：任意异常先查当前状态，只有已是 waiting_approval 才吞，其余原样上抛
            current = store.get_task_state(identity)
            if current != "waiting_approval":
                raise

    findings = store.list_findings(trace_id)
    if action == "review_publish":
        from apps.code_review_pipeline.approval import approve_task

        github = build_github_client()
        try:
            outcome = await approve_task(
                store,
                github,
                identity,
                trace_id,
                operator=operator_id,
                summary=(
                    f"moa code review: `{_review_task_id(trace_id)}`\n\n"
                    f"人工已逐条标注 {status.total_findings} 条 finding。"
                ),
                findings=_review_finding_dicts(findings),
                require_feedback=True,
                dry_run=settings.code_review_publish_dry_run,
                expected_review_run_id=record.review_run_id,
            )
        except Exception as exc:  # noqa: BLE001 - review 写回：任意失败告知用户并停止成功路径，不能假装已写回
            logger.exception("review publish failed trace=%s", trace_id)
            await _safe_send(session_id, f"写回 GitHub 失败：{exc}", trace_id)
            return
        finally:
            close = getattr(github, "aclose", None)
            if close is not None:
                await close()
        if outcome.status == "dry_run":
            message = "审批已通过，但 CODE_REVIEW_PUBLISH_DRY_RUN 已开启；本次未写回 GitHub"
        else:
            message = (
                "已批准并写回 GitHub"
                f"（{outcome.status}, review={outcome.review_id or '-'}）"
            )
        await _safe_send(session_id, message, trace_id)
        return

    from apps.code_review_pipeline.approval import reject_task

    await reject_task(
        store,
        identity,
        trace_id,
        operator=operator_id,
        reason="rejected from Feishu finding review",
        expected_review_run_id=record.review_run_id,
    )
    await _safe_send(session_id, "已拒绝，未写回 GitHub", trace_id)


async def _process_review_card_action_serialized(**kwargs) -> None:
    async with _review_action_lock:
        await _process_review_card_action(**kwargs)


async def _record_review_feedback(
    *,
    store,
    record,
    value: dict,
    operator_id: str,
    session_id: str,
    event_id: str,
    message_id: str = "",
) -> None:
    from apps.code_review_pipeline.schemas.feedback import FindingFeedback

    disposition = str(value.get("disposition") or "")
    finding_uid = str(value.get("finding_uid") or "")
    reason_code = str(value.get("reason_code") or "")
    if not finding_uid:
        await _safe_send(session_id, "卡片缺少 finding_uid，已忽略", record.trace_id)
        return
    if not event_id:
        await _safe_send(
            session_id,
            "卡片回调缺少 event_id，无法保证反馈幂等，已忽略",
            record.trace_id,
        )
        return
    callback_event_id = event_id
    try:
        feedback = FindingFeedback(
            event_id=callback_event_id,
            trace_id=record.trace_id,
            finding_uid=finding_uid,
            disposition=disposition,
            reason_code=reason_code,
            actor=operator_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            review_run_id=record.review_run_id,
        )
        inserted = store.record_feedback(feedback)
    except ValueError as exc:
        await _safe_send(session_id, f"反馈未保存：{exc}", record.trace_id)
        return
    if not inserted:
        logger.info("duplicate finding feedback event=%s trace=%s", callback_event_id, record.trace_id)
        return

    status = store.feedback_status(record.trace_id)
    if message_id:
        from apps.code_review_pipeline.notifications.feishu_notifier import (
            FeishuReviewNotifier,
        )

        updated = await FeishuReviewNotifier.from_env().update_feedback_card(
            message_id=message_id,
            trace_id=record.trace_id,
            repo=record.repo,
            pr_number=record.pr_number,
            head_sha=record.head_sha,
            review_run_id=record.review_run_id,
            findings=store.list_findings(record.trace_id),
            feedback_by_uid={
                uid: event.disposition
                for uid, event in status.latest_human_by_finding.items()
            },
            page=int(value.get("page") or 1),
            total_pages=int(value.get("total_pages") or 1),
        )
        if not updated:
            logger.warning("feedback card update failed trace=%s", record.trace_id)
    if status.complete:
        from apps.code_review_pipeline.notifications.feishu_notifier import (
            FeishuReviewNotifier,
        )

        sent = await FeishuReviewNotifier.from_env().send_publish_control(
            trace_id=record.trace_id,
            repo=record.repo,
            pr_number=record.pr_number,
            head_sha=record.head_sha,
            review_run_id=record.review_run_id,
        )
        if not sent:
            logger.warning("publish control card not sent trace=%s", record.trace_id)
            await _safe_send(
                session_id,
                "标注已保存，但发布控制卡发送失败；请重新点击任一 finding 标注按钮以重试",
                record.trace_id,
            )
    else:
        await _safe_send(
            session_id,
            f"已记录，还差 {status.total_findings - status.labelled_findings} 条 finding",
            record.trace_id,
        )

@router.post("/feishu/event")
async def feishu_event(request: Request):
    # 卡片回调有 3 秒响应窗口，处理耗时必须可观测（否则只能猜是不是网络）。
    started = time.monotonic()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 请求体解析：任意解析失败返回 400，回调端点不能 500
        return JSONResponse({"error":"invalid_json"}, status_code=400)
    if isinstance(body, dict) and body.get("encrypt"):
        # 加密模式未实现（见 README 已知边界）。此前这种请求落到 "ignored" 分支被**静默丢弃**
        # ——配了加密后收不到任何事件、日志里也看不出原因。现在明确报错（2026-09-23）。
        logger.error(
            "feishu_event 收到加密事件（body.encrypt）但加密模式未实现："
            "请在开放平台关闭加密，或先实现 AES 解密 + X-Lark-Signature 校验"
        )
        return JSONResponse({"error": "encrypted_events_unsupported"}, status_code=400)
    if not verify_verification_token(
        body,
        settings.feishu_verification_token,
        allow_insecure=insecure_mode_enabled(settings.gateway_allow_insecure),
    ):
        # 此前本函数只看顶层 token，而 v2 的 token 在 header.token —— 于是"配了也
        # 从不比对"，等于这道门一直开着（2026-09-23 修）。拒绝时打点便于现网定位：
        # 若真实回调被拒，说明平台带的字段位置与我实现的不一致。
        logger.warning(
            "feishu_event rejected by token check: has_top_token=%s has_header_token=%s configured=%s",
            isinstance(body.get("token"), str) and bool(body.get("token")),
            isinstance(body.get("header"), dict) and bool(body["header"].get("token")),
            bool(settings.feishu_verification_token),
        )
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    header = body.get("header", {}) if isinstance(body.get("header"), dict) else {}
    event_id = header.get("event_id", body.get("event_id", ""))
    if event_id and not _dedup(event_id):
        # 卡片回调必须回**空对象**（2026-09-29 修）：返回 {"msg": ...} 会被客户端当成
        # 卡片更新去解析、解析失败即报 200341。而重投很容易走到这里——第一次响应慢、
        # 平台重投同一条，第二投就命中本分支。此处事件类型还没解析，但对消息类事件
        # 回 {} 同样无害（客户端不看 body）。
        logger.info("feishu_event duplicate event_id=%s（重投，已忽略）", event_id)
        return JSONResponse({})

    parsed = parse_feishu_event(body)
    logger.info("feishu_event_parsed event_type=%s body=%s", parsed["event_type"], json.dumps(body, ensure_ascii=False))

    if parsed["event_type"] == "url_verification":
        return JSONResponse({"challenge": parsed["challenge"]})
    if parsed["event_type"] == "card_action":
        action_value = parsed.get("action", {})
        action = action_value.get("action", "")
        trace_id = action_value.get("trace_id", "")
        session_id = parsed.get("chat_id", "")
        logger.info("card_action action=%s trace_id=%s chat_id=%s", action, trace_id, session_id)

        if action in ("approve", "reject"):
            # 飞书卡片回调有 3 秒响应窗口：真正的 HITL 闭环（FSM 推进、输出
            # 送达、审计留痕）必须异步执行、响应先行——在响应前 await 飞书
            # API 发消息会撑爆窗口，客户端报 200341"出错了，请稍后重试"。
            task = asyncio.create_task(
                _process_hitl_card(action, trace_id, session_id, parsed.get("operator_id") or "")
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        elif action in ("review_finding_feedback", "review_publish", "review_reject"):
            task = asyncio.create_task(
                _process_review_card_action_serialized(
                    action=action,
                    value=action_value,
                    operator_id=parsed.get("operator_id") or "",
                    session_id=session_id,
                    event_id=str(parsed.get("event_id") or body.get("event_id") or ""),
                    message_id=str(parsed.get("message_id") or ""),
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        else:
            logger.warning("card_action 未知动作，已忽略 action=%r trace=%s", action, trace_id)
        # 卡片回调响应契约：HTTP 200 + 空对象即"已受理"；返回业务结构会被
        # 客户端当作卡片更新解析失败，同样触发 200341。
        # elapsed 是**服务端**受理耗时。飞书窗口是 3000ms——若这里只有几十毫秒而客户端
        # 仍报 200341，那就是网络往返（Funnel 多一跳）吃掉了窗口，不是服务端卡住；
        # 这条日志就是为了把这两种情况分开，而不是继续猜。
        logger.info(
            "card_action 已受理 action=%s elapsed_ms=%.1f（飞书窗口 3000ms）",
            action, (time.monotonic() - started) * 1000,
        )
        return JSONResponse({})
    if parsed["event_type"] not in ("event_callback", "im.message.receive_v1"):
        return JSONResponse({"msg":"ignored"})
    if not parsed["text"] or not parsed["chat_id"]:
        return JSONResponse({"msg":"no_content"})

    sid = parsed["chat_id"]
    moa_event = MoAEvent(
        trace_id=new_trace_id(), event=FsmEvent.MESSAGE_RECEIVED,
        session_id=sid, text=parsed["text"],
        context={"source": "feishu_event"},
        user_id=parsed.get("sender_id", ""),
    )
    try:
        result = await pipeline.run(moa_event, channel="feishu", target=sid, request=request)
    except Exception:  # noqa: BLE001 - 管线执行：任意失败转 error 结果并继续走回复路径
        import traceback
        traceback.print_exc()
        logger.exception("pipeline.run failed for trace_id=%s", moa_event.trace_id)
        result = PipelineResult(
            trace_id=moa_event.trace_id, state="", intent="", text="抱歉，处理消息时出错了",
            status="error", error_code=ErrorCode.INTERNAL_ERROR.value,
        )
    logger.info(
        "pipeline.run result trace_id=%s status=%s code=%s intent=%s text=%s",
        result.trace_id, result.status, result.error_code or "-", result.intent, result.text,
    )

    if result.status == "pending_review":
        reply = "输出需要人工审批"
    elif result.status == "suspended":
        reply = "已检测到敏感内容，消息已挂起"
    elif result.status == "reset":
        reply = result.text
    elif result.status == "error":
        reply = "抱歉，处理消息时出错了"
    else:
        reply = result.text

    adp = await get_adapter()
    if adp:
        await adp.send(ChannelMessage(channel="feishu", target=sid, text=reply, trace_id=result.trace_id))
    return JSONResponse({"msg":"ok"})


async def _handle_merge_approval(
    *,
    hitl_id: str,
    action: str,
    operator_id: str,
    session_id: str,
    trace_id: str,
    created_at: float = 0.0,
) -> JSONResponse:
    """ADR-021 合并审批回调：执行合并、写审计、**把结果回给发起人**。

    **不走** ``engine.decide_hitl()``：合并审批是独立的写操作，与聊天 FSM 的
    会话状态无关。

    ``session_id`` 是卡片回调里的 ``chat_id``（见 ``feishu_event`` 的
    ``parsed["chat_id"]``），可以直接当 ``_safe_send`` 的收件人。此前本函数收下它
    却从不发消息，成功路径只返回一个飞书不认识的 JSON——人点了"批准"，合并确实
    执行了，但**界面上什么也不会发生**（2026-10-09 实测）。
    """
    from apps.code_review_pipeline.task_audit import record_human_decision

    executor = MergeExecutor(
        merge_store,
        build_github_client(),
        run_store=delivery_run_store,
        audit=WalDeliveryAudit(),
    )
    try:
        outcome = await executor.execute(hitl_id, action, operator_id)
    except Exception as exc:  # noqa: BLE001 - 合并执行：任意失败记录并回告用户，审批结果不能静默
        logger.exception("merge approval execution failed: %s", exc)
        await _safe_send(session_id, f"⚠️ 合并执行失败：{exc}", trace_id)
        return JSONResponse(
            {"error": "merge_execution_failed", "message": str(exc)},
            status_code=500,
        )

    # 写审计：谁批的、批了什么、从挂起到决定花了多久。duration 与
    # /webhook/callback 同一语义——dashboard 的"人工审批耗时分布"读的就是它。
    duration_ms = (
        round((time.time() - created_at) * 1000, 1) if created_at > 0 else 0.0
    )
    try:
        await record_human_decision(
            task_id=hitl_id,
            repo=hitl_id.split("#")[0],
            operator=operator_id,
            decision=action,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001 - 人工决策审计：审计旁路任意失败只记录，不影响合并结果
        logger.exception("failed to record human decision for %s", hitl_id)

    await _resolve_merge_card(hitl_id, outcome, operator_id)

    if outcome.merged:
        logger.info("merge approval done: %s merged as %s", hitl_id, outcome.sha)
        await _safe_send(
            session_id,
            f"✅ 已批准，PR 已合并\n{hitl_id}\nmerge sha: {outcome.sha}",
            trace_id,
        )
        return JSONResponse({
            "trace_id": trace_id,
            "status": "merged",
            "sha": outcome.sha,
        })
    if outcome.status == "rejected":
        logger.info("merge approval done: %s rejected by %s", hitl_id, operator_id)
        await _safe_send(session_id, f"❌ 已拒绝，未合并\n{hitl_id}", trace_id)
    else:
        logger.warning("merge approval failed: %s status=%s", hitl_id, outcome.status)
        await _safe_send(
            session_id,
            f"⚠️ 合并失败：{outcome.message or outcome.status}\n{hitl_id}",
            trace_id,
        )
    return JSONResponse({
        "trace_id": trace_id,
        "status": outcome.status,
        "message": outcome.message,
    })


def _merge_notifier() -> MergeNotifier:
    return MergeNotifier(
        default_target=settings.code_review_merge_channel or None
    )


def _parse_merge_identity(hitl_id: str) -> tuple[str, int, str] | None:
    """Parse ``owner/repo#123@sha`` using the **last** ``@`` and ``#``."""
    head, at_sep, sha = hitl_id.rpartition("@")
    repo, hash_sep, pr_str = head.rpartition("#")
    if (
        not at_sep
        or not hash_sep
        or not repo
        or not pr_str
        or not sha
        or not pr_str.isdigit()
    ):
        logger.warning("cannot parse merge identity from hitl_id=%s", hitl_id)
        return None
    try:
        pr_number = int(pr_str)
    except ValueError:
        logger.warning("cannot parse merge identity from hitl_id=%s", hitl_id)
        return None
    return repo, pr_number, sha


async def _resolve_merge_card(
    hitl_id: str, outcome: Any, operator_id: str
) -> None:
    """Retire the approval card after the decision. Display-only, never raises."""
    identity = _parse_merge_identity(hitl_id)
    if identity is None:
        return
    try:
        record = merge_store.get(identity)
    except Exception:  # noqa: BLE001 - 改卡前的读记录失败只告警，不影响审批结果
        logger.warning(
            "failed to read merge record for %s", hitl_id, exc_info=True
        )
        return
    if record is None:
        logger.warning("no merge record for %s; skip resolved card", hitl_id)
        return

    if outcome.merged:
        resolved, detail = "merged", outcome.sha
    elif outcome.status == "rejected":
        resolved, detail = "rejected", operator_id
    else:
        resolved, detail = "failed", outcome.message

    try:
        await _merge_notifier().send_resolved_card(record, resolved, detail)
    except Exception:  # noqa: BLE001 - 改卡是展示层：异常只告警，不改变审批响应
        logger.warning(
            "failed to resolve merge card for %s", hitl_id, exc_info=True
        )
