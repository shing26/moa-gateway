"""飞书远程推送审批（A1）。

推送审批此前只有终端端口
（``apps.code_review_pipeline.delivery.approval_port.TerminalPushApprovalPort``）：
非 TTY、服务器或 CI 上一律拒绝，第三个人无法在终端外批准。本模块把同一条
``PushApprovalPort`` 协议接到飞书上，但**不改变交付流程语义**：

* CLI 仍在 ``READY_FOR_PUSH_APPROVAL`` 等待；
* 批准 → 返回 ``ChangeApproval``，由 CLI 用操作者 PAT 推送；
* 拒绝 → 返回 ``None``，既有 flow 落 ``REJECTED``；
* 等待窗口到期 → 抛 ``PushApprovalTimedOut``，超时不是人的决定。

跨进程靠 Redis：CLI 写 pending 记录并发卡，飞书回调原子认领后写 decision，
CLI 轮询 decision。Redis 降级为进程内存时拒绝启用——跨进程看不见的审批
等于没有审批。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.engine import HitlRequest
from app.middleware.auth import insecure_mode_enabled
from apps.code_review_pipeline.delivery.ports import PushApprovalTimedOut

logger = logging.getLogger("moa.delivery.feishu_approval")

PUSH_APPROVAL_KIND = "push_approval"
DECISION_KEY_PREFIX = "moa:delivery_push_decision"
_PROBE_SUFFIX = "__probe__"


class PushApprovalUnavailable(RuntimeError):
    """配置或共享存储不满足远程审批条件（flow 映射为 INFRA_ERROR）。"""


def decision_key(hitl_id: str) -> str:
    return f"{DECISION_KEY_PREFIX}:{hitl_id}"


def _storage_of(session_store: Any) -> Any | None:
    return getattr(session_store, "_storage", None)


def shared_storage_available(session_store: Any) -> bool:
    """探测 HITL 存储是否真的跨进程可用。

    ``RedisHitlStorage.using_memory`` 只在第一次真实操作后才会反映连通性
    （见 ``app/engine.py`` 的同名注释），所以这里先触发一次读再判断。
    """
    storage = _storage_of(session_store)
    if storage is None:
        return False
    try:
        storage.get(f"{DECISION_KEY_PREFIX}:{_PROBE_SUFFIX}")
    except Exception:  # noqa: BLE001 - 探测失败等价于不可用，审批必须 fail-closed
        logger.exception("delivery push approval storage probe failed")
        return False
    return not bool(getattr(storage, "using_memory", False))


def write_decision(
    session_store: Any,
    hitl_id: str,
    decision: dict[str, Any],
    *,
    ttl_s: int,
) -> bool:
    """把审批决定写进共享存储；存储不可跨进程时返回 ``False``。"""
    storage = _storage_of(session_store)
    if storage is None:
        return False
    if not shared_storage_available(session_store):
        return False
    storage.set(
        decision_key(hitl_id),
        json.dumps(decision, ensure_ascii=False),
        ttl=max(60, int(ttl_s)),
    )
    return True


def read_decision(session_store: Any, hitl_id: str) -> dict[str, Any] | None:
    storage = _storage_of(session_store)
    if storage is None:
        return None
    raw = storage.get(decision_key(hitl_id))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("delivery push approval decision corrupt hitl_id=%s", hitl_id)
        return None
    return payload if isinstance(payload, dict) else None


@dataclass
class PushApprovalCard:
    """飞书审批卡。鸭子类型满足 ``FeishuCardSender.send_card`` 的调用面。"""

    session_id: str
    trace_id: str
    target: str
    run_id: str
    repo: str
    branch: str
    bundle_hash: str
    patch_hash: str
    verify_exit_code: int
    file_count: int
    expires_at: str
    applicant: str = ""
    channel: str = "feishu"

    def _lines(self) -> list[str]:
        lines = [
            f"**Run**: {self.run_id}",
            f"**仓库**: {self.repo}",
            f"**分支**: {self.branch}",
            f"**Bundle**: `{self.bundle_hash}`",
            f"**Patch**: `{self.patch_hash}`",
            f"**验证退出码**: {self.verify_exit_code}",
            f"**变更文件**: {self.file_count}",
            f"**过期**: {self.expires_at}",
        ]
        if self.applicant:
            lines.append(f"**操作者**: {self.applicant}")
        return lines

    def to_card_payload(self) -> dict[str, Any]:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "Agent Gateway - 推送审批",
                },
                "template": "orange",
            },
            "elements": [
                {"tag": "markdown", "content": "\n".join(self._lines())},
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "批准推送"},
                            "value": {
                                "action": "approve",
                                "session_id": self.session_id,
                                "trace_id": self.trace_id,
                            },
                            "type": "primary",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "拒绝"},
                            "value": {
                                "action": "reject",
                                "session_id": self.session_id,
                                "trace_id": self.trace_id,
                            },
                            "type": "danger",
                        },
                    ],
                },
            ],
        }

    def to_message_payload(self) -> dict[str, Any]:
        return {
            "receive_id": self.target,
            "msg_type": "interactive",
            "content": json.dumps(self.to_card_payload(), ensure_ascii=False),
        }


class FeishuPushApprovalPort:
    """``PushApprovalPort`` 的飞书实现（阻塞等待 + 跨进程 decision key）。"""

    def __init__(
        self,
        *,
        card_sender: Any,
        session_store: Any,
        run_store: Any,
        github: Any,
        settings_obj: Any,
        poll_interval_s: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._card_sender = card_sender
        self._session_store = session_store
        self._run_store = run_store
        self._github = github
        self._settings = settings_obj
        self._poll_interval_s = max(0.01, float(poll_interval_s))
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.time
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    async def request(self, *, run_id: str, bundle: Any) -> Any:
        cfg = self._settings
        if not bool(getattr(cfg, "feishu_push_approval_enabled", False)):
            raise PushApprovalUnavailable(
                "FEISHU_PUSH_APPROVAL_ENABLED=0；飞书推送审批端口不应被启用"
            )
        target = str(getattr(cfg, "delivery_approval_chat_id", "") or "").strip()
        if not target:
            raise PushApprovalUnavailable(
                "DELIVERY_APPROVAL_CHAT_ID（或 CODE_REVIEW_MERGE_CHANNEL）未配置"
            )
        if not (
            str(getattr(cfg, "feishu_app_id", "") or "").strip()
            and str(getattr(cfg, "feishu_app_secret", "") or "").strip()
        ):
            raise PushApprovalUnavailable(
                "FEISHU_APP_ID/FEISHU_APP_SECRET 未配置，无法发送审批卡"
            )
        insecure = insecure_mode_enabled(
            str(getattr(cfg, "gateway_allow_insecure", "") or "")
        )
        if not tuple(getattr(cfg, "hitl_approver_ids", ()) or ()) and not insecure:
            raise PushApprovalUnavailable(
                "HITL_APPROVER_IDS 未配置，且未显式 GATEWAY_ALLOW_INSECURE=1；"
                "拒绝启用飞书推送审批"
            )
        if not str(getattr(cfg, "feishu_operator_open_id", "") or "").strip() and not insecure:
            raise PushApprovalUnavailable(
                "FEISHU_OPERATOR_OPEN_ID 未配置，无法验证 approver ≠ operator"
            )
        if not shared_storage_available(self._session_store):
            raise PushApprovalUnavailable(
                "HITL 存储未连接共享 Redis（进程内存降级）；"
                "飞书推送审批需要 CLI 与回调服务共享同一份存储"
            )

        operator_login = str(await self._github.get_authenticated_login())
        try:
            run = self._run_store.load(run_id)
        except Exception as exc:  # noqa: BLE001 - 读不到 run 是基础设施错误，不能伪造一条审批
            raise PushApprovalUnavailable(
                f"无法读取 DeliveryRun {run_id!r}: {type(exc).__name__}: {exc}"
            ) from exc
        repo = str(getattr(getattr(run, "request", None), "repo", "") or "")

        timeout_s = max(1, int(getattr(cfg, "feishu_push_approval_timeout_s", 1800)))
        hitl_id = str(self._id_factory())
        created_at = float(self._clock())
        expires_at = datetime.fromtimestamp(
            created_at + timeout_s, tz=timezone.utc
        ).isoformat()
        payload = {
            "run_id": run_id,
            "repo": repo,
            "branch": str(getattr(bundle, "branch", "")),
            "bundle_hash": str(getattr(bundle, "bundle_hash", "")),
            "patch_hash": str(getattr(bundle, "patch_hash", "")),
            "verify_exit_code": int(getattr(bundle, "verify_exit_code", -1)),
            "file_count": len(getattr(bundle, "files", ()) or ()),
            "operator_login": operator_login,
            "expires_at": expires_at,
        }
        card = PushApprovalCard(
            session_id=f"delivery:{run_id}",
            # 卡片按钮回传的 trace_id 就是 pending 记录的 key：回调靠它
            # ``pop_hitl`` 认领，所以这里是 hitl_id，不是 bundle.trace_id。
            trace_id=hitl_id,
            target=target,
            run_id=run_id,
            repo=repo,
            branch=payload["branch"],
            bundle_hash=payload["bundle_hash"],
            patch_hash=payload["patch_hash"],
            verify_exit_code=payload["verify_exit_code"],
            file_count=payload["file_count"],
            expires_at=expires_at,
            applicant=operator_login,
        )
        hitl = HitlRequest(
            session_id=f"delivery:{run_id}",
            trace_id=hitl_id,
            agent_output="\n".join(card._lines()),
            intent=PUSH_APPROVAL_KIND,
            agent_name="delivery_push_gate",
            channel="feishu",
            target=target,
            created_at=created_at,
            hitl_kind=PUSH_APPROVAL_KIND,
            applicant=operator_login,
            reason="推送前人工审批",
            delivery_payload=payload,
        )
        # 先登记再发卡：卡片一到用户就可能点，登记晚一步那次点击会落空。
        self._session_store.store_hitl(hitl_id, hitl)
        sent = await self._card_sender.send_card(card)
        if not sent:
            self._session_store.remove_hitl(hitl_id)
            raise PushApprovalUnavailable("飞书推送审批卡发送失败")

        deadline = created_at + timeout_s
        decision: dict[str, Any] | None = None
        while float(self._clock()) < deadline:
            decision = read_decision(self._session_store, hitl_id)
            if decision is not None:
                break
            await self._sleep(self._poll_interval_s)
        if decision is None:
            self._session_store.remove_hitl(hitl_id)
            logger.warning(
                "feishu push approval timed out run_id=%s hitl_id=%s", run_id, hitl_id
            )
            raise PushApprovalTimedOut(
                f"feishu push approval timed out after {timeout_s}s "
                f"run_id={run_id} hitl_id={hitl_id}"
            )

        # 回调已原子认领 pending；这里再清一次是幂等的（超时路径也走同一句）。
        self._session_store.remove_hitl(hitl_id)
        if str(decision.get("decision", "")) != "approve":
            logger.info(
                "feishu push approval rejected run_id=%s reason=%s",
                run_id,
                decision.get("reason", ""),
            )
            return None
        if str(decision.get("bundle_hash", "")) != str(
            getattr(bundle, "bundle_hash", "")
        ):
            logger.warning(
                "feishu push approval bundle mismatch run_id=%s pending=%s current=%s",
                run_id,
                decision.get("bundle_hash", ""),
                getattr(bundle, "bundle_hash", ""),
            )
            return None

        from apps.code_review_pipeline.change.pipeline import ChangeApproval

        return ChangeApproval(
            approver=str(decision.get("approver", "")),
            # DeliveryFlow / ChangePublisher 都以 approval.trace_id ==
            # bundle.trace_id 作为进入推送的绑定条件；hitl_id 只用于
            # pending 记录与 decision key 的关联（修正案 1）。
            trace_id=str(getattr(bundle, "trace_id", "")),
            bundle_hash=str(getattr(bundle, "bundle_hash", "")),
            run_id=run_id,
            channel="feishu",
            identity_verified=True,
            operator_login=operator_login,
            gate=str(decision.get("gate", "")),
        )
