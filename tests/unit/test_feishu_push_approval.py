from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.engine import HitlRequest, SessionStore
from apps.code_review_pipeline.delivery.feishu_approval import (
    PUSH_APPROVAL_KIND,
    FeishuPushApprovalPort,
    PushApprovalCard,
    PushApprovalUnavailable,
    decision_key,
    read_decision,
    write_decision,
)
from apps.code_review_pipeline.delivery.ports import PushApprovalTimedOut
from apps.code_review_pipeline.delivery.push_approval_registry import (
    list_pending,
)


class FakeStorage:
    """最小 Redis 替身：方法与 RedisHitlStorage 的调用面一致。"""

    def __init__(self, *, using_memory: bool = False) -> None:
        self.using_memory = using_memory
        self.data: dict[str, str] = {}

    def key(self, hitl_id: str) -> str:
        return f"moa:hitl:{hitl_id}"

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self.data[key] = value

    def pop(self, key: str) -> str | None:
        return self.data.pop(key, None)

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def clear(self) -> None:
        self.data.clear()


class FakeCardSender:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.cards: list[Any] = []

    async def send_card(self, card: Any) -> bool:
        self.cards.append(card)
        return self.ok


class FakeIdCardSender(FakeCardSender):
    """带 message id 的 sender，并记录终态改卡。"""

    def __init__(
        self,
        *,
        ok: bool = True,
        message_id: str = "om_card_1",
        update_result: bool = True,
        update_error: Exception | None = None,
    ) -> None:
        super().__init__(ok=ok)
        self.message_id = message_id
        self.update_result = update_result
        self.update_error = update_error
        self.updates: list[tuple[str, Any]] = []

    async def send_card_with_id(self, card: Any) -> str:
        self.cards.append(card)
        return self.message_id if self.ok else ""

    async def update_card(self, message_id: str, card: Any) -> bool:
        self.updates.append((message_id, card))
        if self.update_error is not None:
            raise self.update_error
        return self.update_result


class FakeGitHub:
    def __init__(self, login: Any = "alice") -> None:
        self.login = login

    async def get_authenticated_login(self) -> str:
        if isinstance(self.login, Exception):
            raise self.login
        return str(self.login)


class FakeRunStore:
    def __init__(self, run: Any) -> None:
        self.run = run

    def load(self, run_id: str) -> Any:
        if self.run is None or self.run.run_id != run_id:
            raise FileNotFoundError(run_id)
        return self.run


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "feishu_push_approval_enabled": True,
        "feishu_push_approval_timeout_s": 5,
        "feishu_operator_open_id": "ou_operator",
        "delivery_approval_chat_id": "oc_chat",
        "feishu_app_id": "cli_test",
        "feishu_app_secret": "secret",
        "hitl_approver_ids": ("ou_approver",),
        "gateway_allow_insecure": "0",
        "code_review_merge_channel": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _bundle(*, bundle_hash: str = "b" * 64, patch_hash: str = "p" * 64) -> SimpleNamespace:
    return SimpleNamespace(
        branch="moa/a1-test",
        bundle_hash=bundle_hash,
        patch_hash=patch_hash,
        verify_exit_code=0,
        files=(object(), object(), object()),
    )


def _run(repo: str = "shing26/OpsShield") -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run-1",
        request=SimpleNamespace(repo=repo),
        bundle=None,
    )


def _port(
    *,
    store: SessionStore,
    sender: FakeCardSender,
    settings: Any,
    github: FakeGitHub | None = None,
    **kwargs: Any,
) -> FeishuPushApprovalPort:
    return FeishuPushApprovalPort(
        card_sender=sender,
        session_store=store,
        run_store=FakeRunStore(_run()),
        github=github or FakeGitHub(),
        settings_obj=settings,
        id_factory=lambda: "hitl-1",
        poll_interval_s=0.001,
        **kwargs,
    )


async def _wait_for_card(sender: FakeCardSender) -> None:
    for _ in range(200):
        if sender.cards:
            return
        await asyncio.sleep(0)
    raise AssertionError("card was not sent")


async def _wait_for_pending(root: Any, hitl_id: str = "hitl-1") -> dict[str, Any]:
    for _ in range(200):
        for record in list_pending(root):
            if record.get("hitl_id") == hitl_id:
                return record
        await asyncio.sleep(0)
    raise AssertionError("sidecar was not written")


def test_request_fails_closed_when_storage_is_in_memory() -> None:
    sender = FakeCardSender()
    port = FeishuPushApprovalPort(
        card_sender=sender,
        session_store=SessionStore(),  # 无 storage → 进程内存
        run_store=FakeRunStore(_run()),
        github=FakeGitHub(),
        settings_obj=_settings(),
        id_factory=lambda: "hitl-1",
    )

    with pytest.raises(PushApprovalUnavailable, match="共享 Redis"):
        asyncio.run(port.request(run_id="run-1", bundle=_bundle()))

    assert sender.cards == []


def test_request_requires_chat_target_and_credentials() -> None:
    store = SessionStore(storage=FakeStorage())
    port = _port(
        store=store,
        sender=FakeCardSender(),
        settings=_settings(delivery_approval_chat_id=""),
    )
    with pytest.raises(PushApprovalUnavailable, match="DELIVERY_APPROVAL_CHAT_ID"):
        asyncio.run(port.request(run_id="run-1", bundle=_bundle()))

    port = _port(
        store=store,
        sender=FakeCardSender(),
        settings=_settings(feishu_app_id=""),
    )
    with pytest.raises(PushApprovalUnavailable, match="FEISHU_APP_ID"):
        asyncio.run(port.request(run_id="run-1", bundle=_bundle()))


def test_request_refuses_when_operator_identity_cannot_be_verified() -> None:
    store = SessionStore(storage=FakeStorage())
    port = _port(
        store=store,
        sender=FakeCardSender(),
        settings=_settings(feishu_operator_open_id="", gateway_allow_insecure="0"),
    )

    with pytest.raises(PushApprovalUnavailable, match="FEISHU_OPERATOR_OPEN_ID"):
        asyncio.run(port.request(run_id="run-1", bundle=_bundle()))


def test_request_sends_card_and_returns_approval() -> None:
    store = SessionStore(storage=FakeStorage())
    sender = FakeCardSender()
    port = _port(store=store, sender=sender, settings=_settings())

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        card = sender.cards[0]
        assert card.run_id == "run-1"
        assert card.repo == "shing26/OpsShield"
        assert card.bundle_hash == "b" * 64
        assert card.patch_hash == "p" * 64
        assert card.verify_exit_code == 0
        assert card.file_count == 3
        payload = json.loads(card.to_message_payload()["content"])
        body = json.dumps(payload, ensure_ascii=False)
        assert "批准推送" in body and "拒绝" in body
        assert "diff --git" not in body and "secret" not in body

        assert write_decision(
            store,
            "hitl-1",
            {
                "decision": "approve",
                "approver": "ou_approver",
                "channel": "feishu",
                "identity_verified": True,
                "gate": "allowlist",
                "bundle_hash": "b" * 64,
            },
            ttl_s=60,
        )
        return await task

    approval = asyncio.run(scenario())
    assert approval is not None
    assert approval.approver == "ou_approver"
    assert approval.run_id == "run-1"
    assert approval.bundle_hash == "b" * 64
    assert approval.channel == "feishu"
    assert approval.identity_verified is True
    assert approval.operator_login == "alice"
    assert approval.gate == "allowlist"
    assert store.get_hitl("hitl-1") is None


def test_request_returns_none_on_reject() -> None:
    store = SessionStore(storage=FakeStorage())
    sender = FakeCardSender()
    port = _port(store=store, sender=sender, settings=_settings())

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        write_decision(
            store,
            "hitl-1",
            {"decision": "reject", "reason": "rejected_by_human"},
            ttl_s=60,
        )
        return await task

    assert asyncio.run(scenario()) is None
    assert store.get_hitl("hitl-1") is None


def test_request_times_out_raises_and_cleans_pending() -> None:
    """T1：等待到期是超时信号，不是 None，也不写 decision。"""
    store = SessionStore(storage=FakeStorage())
    sender = FakeCardSender()
    clock = SimpleNamespace(now=1000.0)

    def read_clock() -> float:
        return clock.now

    async def advance(seconds: float) -> None:
        clock.now += seconds

    port = _port(
        store=store,
        sender=sender,
        settings=_settings(feishu_push_approval_timeout_s=1),
        clock=read_clock,
        sleep=advance,
    )
    port._poll_interval_s = 0.5

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        return await task

    with pytest.raises(PushApprovalTimedOut, match="timed out"):
        asyncio.run(scenario())
    assert store.get_hitl("hitl-1") is None
    # 超时不是人的决定：decision key 从未被写入。
    assert read_decision(store, "hitl-1") is None


def test_card_send_failure_removes_pending_and_raises() -> None:
    store = SessionStore(storage=FakeStorage())
    port = _port(store=store, sender=FakeCardSender(ok=False), settings=_settings())

    with pytest.raises(PushApprovalUnavailable, match="发送失败"):
        asyncio.run(port.request(run_id="run-1", bundle=_bundle()))
    assert store.get_hitl("hitl-1") is None


def test_approval_with_different_bundle_hash_is_refused() -> None:
    store = SessionStore(storage=FakeStorage())
    sender = FakeCardSender()
    port = _port(store=store, sender=sender, settings=_settings())

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        write_decision(
            store,
            "hitl-1",
            {"decision": "approve", "approver": "ou_approver", "bundle_hash": "x" * 64},
            ttl_s=60,
        )
        return await task

    assert asyncio.run(scenario()) is None


def test_request_writes_sidecar_with_message_id(tmp_path: Any) -> None:
    """T2：拿到 message id → 侧车字段齐全。"""
    store = SessionStore(storage=FakeStorage())
    sender = FakeIdCardSender()
    port = _port(
        store=store,
        sender=sender,
        settings=_settings(),
        registry_dir=tmp_path,
    )

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        record = await _wait_for_pending(tmp_path)
        assert record["run_id"] == "run-1"
        assert record["repo"] == "shing26/OpsShield"
        assert record["branch"] == "moa/a1-test"
        assert record["card_message_id"] == "om_card_1"
        assert record["target"] == "oc_chat"
        assert record["state"] == "pending"
        assert isinstance(record["created_at"], float)
        assert isinstance(record["expires_at"], float)
        write_decision(
            store,
            "hitl-1",
            {
                "decision": "approve",
                "approver": "ou_approver",
                "bundle_hash": "b" * 64,
            },
            ttl_s=60,
        )
        return await task

    assert asyncio.run(scenario()) is not None


def test_sender_without_message_id_does_not_write_sidecar(tmp_path: Any) -> None:
    """T3：sender 只有 bool 契约 → 不写侧车，审批照常。"""
    store = SessionStore(storage=FakeStorage())
    sender = FakeCardSender()
    port = _port(
        store=store,
        sender=sender,
        settings=_settings(),
        registry_dir=tmp_path,
    )

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        write_decision(
            store,
            "hitl-1",
            {
                "decision": "approve",
                "approver": "ou_approver",
                "bundle_hash": "b" * 64,
            },
            ttl_s=60,
        )
        return await task

    assert asyncio.run(scenario()) is not None
    assert list_pending(tmp_path) == []


def test_approve_updates_card_and_clears_sidecar(tmp_path: Any) -> None:
    """T4：批准 → 改 approved 卡、清侧车、返回契约不变。"""
    store = SessionStore(storage=FakeStorage())
    sender = FakeIdCardSender()
    port = _port(
        store=store,
        sender=sender,
        settings=_settings(),
        registry_dir=tmp_path,
    )

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        await _wait_for_pending(tmp_path)
        write_decision(
            store,
            "hitl-1",
            {
                "decision": "approve",
                "approver": "ou_approver",
                "channel": "feishu",
                "identity_verified": True,
                "gate": "allowlist",
                "bundle_hash": "b" * 64,
            },
            ttl_s=60,
        )
        return await task

    approval = asyncio.run(scenario())
    assert approval.approver == "ou_approver"
    assert approval.bundle_hash == "b" * 64
    assert approval.run_id == "run-1"
    assert len(sender.updates) == 1
    message_id, card = sender.updates[0]
    assert message_id == "om_card_1"
    assert card.hitl_kind == "push_resolved_approved"
    assert "✅ 已批准推送" in card.agent_output
    assert "ou_approver" in card.agent_output
    assert list_pending(tmp_path) == []


def test_reject_updates_card_and_clears_sidecar(tmp_path: Any) -> None:
    """T5：拒绝 → 改 rejected 卡，返回 None。"""
    store = SessionStore(storage=FakeStorage())
    sender = FakeIdCardSender()
    port = _port(
        store=store,
        sender=sender,
        settings=_settings(),
        registry_dir=tmp_path,
    )

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        await _wait_for_pending(tmp_path)
        write_decision(
            store,
            "hitl-1",
            {"decision": "reject", "approver": "ou_approver", "reason": "no"},
            ttl_s=60,
        )
        return await task

    assert asyncio.run(scenario()) is None
    assert len(sender.updates) == 1
    assert sender.updates[0][1].hitl_kind == "push_resolved_rejected"
    assert "🚫 已拒绝推送" in sender.updates[0][1].agent_output
    assert list_pending(tmp_path) == []


def test_timeout_updates_card_to_expired_and_still_raises(tmp_path: Any) -> None:
    """T6：超时 → 改 expired 卡、清侧车，仍抛 PushApprovalTimedOut。"""
    store = SessionStore(storage=FakeStorage())
    sender = FakeIdCardSender()
    clock = SimpleNamespace(now=1000.0)

    async def advance(seconds: float) -> None:
        clock.now += seconds

    port = _port(
        store=store,
        sender=sender,
        settings=_settings(feishu_push_approval_timeout_s=1),
        registry_dir=tmp_path,
        clock=lambda: clock.now,
        sleep=advance,
    )
    port._poll_interval_s = 0.5

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        return await task

    with pytest.raises(PushApprovalTimedOut, match="timed out"):
        asyncio.run(scenario())
    assert len(sender.updates) == 1
    assert sender.updates[0][1].hitl_kind == "push_resolved_expired"
    assert "⏰ 审批已失效" in sender.updates[0][1].agent_output
    assert list_pending(tmp_path) == []


def test_bundle_mismatch_updates_card_to_rejected(tmp_path: Any) -> None:
    """R2：bundle 不匹配也是拒绝终态，不能把卡片留成“可点”。"""
    store = SessionStore(storage=FakeStorage())
    sender = FakeIdCardSender()
    port = _port(
        store=store,
        sender=sender,
        settings=_settings(),
        registry_dir=tmp_path,
    )

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        await _wait_for_pending(tmp_path)
        write_decision(
            store,
            "hitl-1",
            {
                "decision": "approve",
                "approver": "ou_approver",
                "bundle_hash": "x" * 64,
            },
            ttl_s=60,
        )
        return await task

    assert asyncio.run(scenario()) is None
    assert sender.updates[0][1].hitl_kind == "push_resolved_rejected"
    assert list_pending(tmp_path) == []


@pytest.mark.parametrize("update_error", [None, RuntimeError("feishu down")])
@pytest.mark.parametrize("update_result", [False, True])
def test_card_update_failure_does_not_change_decision(
    tmp_path: Any, update_result: bool, update_error: Exception | None
) -> None:
    """T7：改卡失败/异常不外泄，批准语义不变。"""
    store = SessionStore(storage=FakeStorage())
    sender = FakeIdCardSender(
        update_result=update_result, update_error=update_error
    )
    port = _port(
        store=store,
        sender=sender,
        settings=_settings(),
        registry_dir=tmp_path,
    )

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        await _wait_for_pending(tmp_path)
        write_decision(
            store,
            "hitl-1",
            {
                "decision": "approve",
                "approver": "ou_approver",
                "bundle_hash": "b" * 64,
            },
            ttl_s=60,
        )
        return await task

    approval = asyncio.run(scenario())
    assert approval is not None
    assert approval.approver == "ou_approver"
    assert len(sender.updates) == 1
    assert list_pending(tmp_path) == []


@pytest.mark.parametrize("update_error", [None, RuntimeError("feishu down")])
@pytest.mark.parametrize("update_result", [False, True])
def test_card_update_failure_does_not_turn_reject_into_approve(
    tmp_path: Any, update_result: bool, update_error: Exception | None
) -> None:
    """T7：拒绝路径改卡失败仍是 None。"""
    store = SessionStore(storage=FakeStorage())
    sender = FakeIdCardSender(
        update_result=update_result, update_error=update_error
    )
    port = _port(
        store=store,
        sender=sender,
        settings=_settings(),
        registry_dir=tmp_path,
    )

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        await _wait_for_pending(tmp_path)
        write_decision(store, "hitl-1", {"decision": "reject"}, ttl_s=60)
        return await task

    assert asyncio.run(scenario()) is None
    assert len(sender.updates) == 1
    assert list_pending(tmp_path) == []


@pytest.mark.parametrize("update_error", [None, RuntimeError("feishu down")])
@pytest.mark.parametrize("update_result", [False, True])
def test_card_update_failure_does_not_turn_timeout_into_success(
    tmp_path: Any, update_result: bool, update_error: Exception | None
) -> None:
    """T7：超时路径改卡失败仍抛 PushApprovalTimedOut。"""
    store = SessionStore(storage=FakeStorage())
    sender = FakeIdCardSender(
        update_result=update_result, update_error=update_error
    )
    clock = SimpleNamespace(now=1000.0)

    async def advance(seconds: float) -> None:
        clock.now += seconds

    port = _port(
        store=store,
        sender=sender,
        settings=_settings(feishu_push_approval_timeout_s=1),
        registry_dir=tmp_path,
        clock=lambda: clock.now,
        sleep=advance,
    )
    port._poll_interval_s = 0.5

    async def scenario() -> Any:
        task = asyncio.create_task(port.request(run_id="run-1", bundle=_bundle()))
        await _wait_for_card(sender)
        return await task

    with pytest.raises(PushApprovalTimedOut, match="timed out"):
        asyncio.run(scenario())
    assert len(sender.updates) == 1
    assert list_pending(tmp_path) == []


def test_github_identity_failure_propagates() -> None:
    store = SessionStore(storage=FakeStorage())
    sender = FakeCardSender()
    port = _port(
        store=store,
        sender=sender,
        settings=_settings(),
        github=FakeGitHub(login=RuntimeError("github down")),
    )

    with pytest.raises(RuntimeError, match="github down"):
        asyncio.run(port.request(run_id="run-1", bundle=_bundle()))
    assert sender.cards == []


def test_card_payload_has_only_metadata() -> None:
    card = PushApprovalCard(
        session_id="delivery:run-1",
        trace_id="hitl-1",
        target="oc_chat",
        run_id="run-1",
        repo="shing26/OpsShield",
        branch="moa/a1-test",
        bundle_hash="b" * 64,
        patch_hash="p" * 64,
        verify_exit_code=0,
        file_count=2,
        expires_at="2026-11-20T12:00:00+00:00",
        applicant="alice",
    )
    body = json.dumps(card.to_card_payload(), ensure_ascii=False)
    for expected in ("run-1", "shing26/OpsShield", "moa/a1-test", "b" * 64, "p" * 64):
        assert expected in body
    assert "diff --git" not in body
    assert "BEGIN RSA" not in body


def test_hitl_request_without_delivery_payload_still_deserializes() -> None:
    request = HitlRequest(
        session_id="s",
        trace_id="t",
        agent_output="o",
        intent="i",
        agent_name="a",
        channel="feishu",
        target="x",
    )
    assert request.delivery_payload == {}


# ── 回调侧 ───────────────────────────────────────────────────────────────


def _callback_env(monkeypatch: pytest.MonkeyPatch, store: SessionStore, **overrides: Any) -> None:
    import app.routes.feishu as feishu_route
    from app.config import settings

    run = SimpleNamespace(bundle=SimpleNamespace(bundle_hash="b" * 64))

    class CallbackRunStore:
        def load(self, run_id: str) -> Any:
            if run_id != "run-1":
                raise FileNotFoundError(run_id)
            return run

    monkeypatch.setattr(feishu_route, "engine", SimpleNamespace(session_store=store))
    monkeypatch.setattr(feishu_route, "delivery_run_store", CallbackRunStore())
    monkeypatch.setattr(settings, "hitl_approver_ids", ("ou_approver",))
    monkeypatch.setattr(settings, "feishu_operator_open_id", "ou_operator")
    monkeypatch.setattr(settings, "gateway_allow_insecure", "0")
    monkeypatch.setattr(settings, "feishu_push_approval_timeout_s", 60)
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


def _store_push_hitl(
    store: SessionStore,
    *,
    hitl_id: str = "hitl-1",
    bundle_hash: str = "b" * 64,
    target: str = "oc_chat",
) -> None:
    store.store_hitl(
        hitl_id,
        HitlRequest(
            session_id="delivery:run-1",
            trace_id=hitl_id,
            agent_output="推送审批",
            intent=PUSH_APPROVAL_KIND,
            agent_name="delivery_push_gate",
            channel="feishu",
            target=target,
            created_at=time.time(),
            hitl_kind=PUSH_APPROVAL_KIND,
            delivery_payload={"run_id": "run-1", "bundle_hash": bundle_hash},
        ),
    )


def _patch_side_effects(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    import app.routes.feishu as feishu_route

    sent: list[tuple[str, str]] = []

    async def fake_send(target: str, text: str, trace_id: str) -> None:
        sent.append((target, text))

    async def fake_log(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(feishu_route, "_safe_send", fake_send)
    monkeypatch.setattr(feishu_route, "log_request", fake_log)
    return sent


@pytest.mark.asyncio
async def test_callback_approve_writes_decision_and_replies_to_card_target(monkeypatch) -> None:
    import app.routes.feishu as feishu_route

    store = SessionStore(storage=FakeStorage())
    _callback_env(monkeypatch, store)
    sent = _patch_side_effects(monkeypatch)
    _store_push_hitl(store)

    await feishu_route._process_hitl_card("approve", "hitl-1", "delivery:run-1", "ou_approver")

    decision = read_decision(store, "hitl-1")
    assert decision is not None
    assert decision["decision"] == "approve"
    assert decision["approver"] == "ou_approver"
    assert decision["channel"] == "feishu"
    assert decision["identity_verified"] is True
    assert decision["gate"] == "allowlist"
    assert decision["bundle_hash"] == "b" * 64
    assert store.get_hitl("hitl-1") is None
    assert sent and sent[0][0] == "oc_chat"


@pytest.mark.asyncio
async def test_callback_outsider_is_refused_without_consuming(monkeypatch) -> None:
    import app.routes.feishu as feishu_route

    store = SessionStore(storage=FakeStorage())
    _callback_env(monkeypatch, store)
    sent = _patch_side_effects(monkeypatch)
    _store_push_hitl(store)

    await feishu_route._process_hitl_card("approve", "hitl-1", "delivery:run-1", "ou_outsider")

    assert read_decision(store, "hitl-1") is None
    assert store.get_hitl("hitl-1") is not None
    assert sent and "权限" in sent[0][1]


@pytest.mark.asyncio
async def test_callback_self_approval_is_rejected(monkeypatch) -> None:
    import app.routes.feishu as feishu_route

    store = SessionStore(storage=FakeStorage())
    _callback_env(monkeypatch, store, feishu_operator_open_id="ou_approver")
    _patch_side_effects(monkeypatch)
    _store_push_hitl(store)

    await feishu_route._process_hitl_card("approve", "hitl-1", "delivery:run-1", "ou_approver")

    decision = read_decision(store, "hitl-1")
    assert decision is not None
    assert decision["decision"] == "reject"
    assert decision["reason"] == "self_approval"


@pytest.mark.asyncio
async def test_callback_missing_operator_open_id_fails_closed(monkeypatch) -> None:
    import app.routes.feishu as feishu_route

    store = SessionStore(storage=FakeStorage())
    _callback_env(monkeypatch, store, feishu_operator_open_id="")
    _patch_side_effects(monkeypatch)
    _store_push_hitl(store)

    await feishu_route._process_hitl_card("approve", "hitl-1", "delivery:run-1", "ou_approver")

    decision = read_decision(store, "hitl-1")
    assert decision is not None
    assert decision["reason"] == "operator_unverified"


@pytest.mark.asyncio
async def test_callback_bundle_mismatch_is_rejected(monkeypatch) -> None:
    import app.routes.feishu as feishu_route

    store = SessionStore(storage=FakeStorage())
    _callback_env(monkeypatch, store)
    _patch_side_effects(monkeypatch)
    _store_push_hitl(store, bundle_hash="a" * 64)

    await feishu_route._process_hitl_card("approve", "hitl-1", "delivery:run-1", "ou_approver")

    decision = read_decision(store, "hitl-1")
    assert decision is not None
    assert decision["decision"] == "reject"
    assert decision["reason"] == "bundle_mismatch"


@pytest.mark.asyncio
async def test_callback_double_click_only_applies_once(monkeypatch) -> None:
    import app.routes.feishu as feishu_route

    store = SessionStore(storage=FakeStorage())
    _callback_env(monkeypatch, store)
    sent = _patch_side_effects(monkeypatch)
    _store_push_hitl(store)

    await feishu_route._process_hitl_card("approve", "hitl-1", "delivery:run-1", "ou_approver")
    first = read_decision(store, "hitl-1")
    await feishu_route._process_hitl_card("reject", "hitl-1", "delivery:run-1", "ou_approver")

    assert first is not None and first["decision"] == "approve"
    assert read_decision(store, "hitl-1") == first
    assert any("已失效或已被处理" in text for _, text in sent)


@pytest.mark.asyncio
async def test_callback_storage_unavailable_does_not_write(monkeypatch) -> None:
    import app.routes.feishu as feishu_route

    store = SessionStore(storage=FakeStorage(using_memory=True))
    _callback_env(monkeypatch, store)
    sent = _patch_side_effects(monkeypatch)
    _store_push_hitl(store)

    await feishu_route._process_hitl_card("approve", "hitl-1", "delivery:run-1", "ou_approver")

    assert read_decision(store, "hitl-1") is None
    assert any("审批存储不可用" in text for _, text in sent)


def test_decision_key_is_namespaced() -> None:
    assert decision_key("abc") == "moa:delivery_push_decision:abc"


@pytest.mark.asyncio
async def test_concurrent_callbacks_write_exactly_one_decision(monkeypatch) -> None:
    """两个 callback 协程真并发到达时，``pop_hitl`` 只让一个写入 decision。"""
    import app.routes.feishu as feishu_route

    store = SessionStore(storage=FakeStorage())
    _callback_env(monkeypatch, store)
    sent: list[tuple[str, str]] = []

    async def yielding_send(target: str, text: str, trace_id: str) -> None:
        # 制造一次真实的调度让步：让两个回调在写 decision 之前交错执行。
        await asyncio.sleep(0)
        sent.append((target, text))

    async def fake_log(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(feishu_route, "_safe_send", yielding_send)
    monkeypatch.setattr(feishu_route, "log_request", fake_log)
    _store_push_hitl(store)

    real_write = feishu_route.write_decision
    writes: list[str] = []

    def counting_write(
        session_store: Any,
        hitl_id: str,
        decision: dict[str, Any],
        *,
        ttl_s: int,
    ) -> bool:
        writes.append(str(decision.get("decision", "")))
        return bool(real_write(session_store, hitl_id, decision, ttl_s=ttl_s))

    monkeypatch.setattr(feishu_route, "write_decision", counting_write)

    await asyncio.gather(
        feishu_route._process_hitl_card(
            "approve", "hitl-1", "delivery:run-1", "ou_approver"
        ),
        feishu_route._process_hitl_card(
            "reject", "hitl-1", "delivery:run-1", "ou_approver"
        ),
    )

    assert len(writes) == 1
    decision = read_decision(store, "hitl-1")
    assert decision is not None
    assert decision["decision"] == writes[0]
    assert any("已失效或已被处理" in text for _, text in sent)


@pytest.mark.asyncio
async def test_callback_insecure_self_approval_gate_is_recorded(monkeypatch) -> None:
    """显式 insecure 放行自审批时，审计 gate 必须如实标记 ``insecure_self_approval``。"""
    import app.routes.feishu as feishu_route

    store = SessionStore(storage=FakeStorage())
    _callback_env(
        monkeypatch,
        store,
        gateway_allow_insecure="1",
        feishu_operator_open_id="ou_approver",
    )
    _patch_side_effects(monkeypatch)
    _store_push_hitl(store)

    await feishu_route._process_hitl_card(
        "approve", "hitl-1", "delivery:run-1", "ou_approver"
    )

    decision = read_decision(store, "hitl-1")
    assert decision is not None
    assert decision["decision"] == "approve"
    assert decision["gate"] == "insecure_self_approval"


@pytest.mark.asyncio
async def test_callback_insecure_unverified_operator_gate_is_recorded(monkeypatch) -> None:
    """未配置 operator 且显式 insecure 时，gate 必须标记 ``insecure_operator_unverified``。"""
    import app.routes.feishu as feishu_route

    store = SessionStore(storage=FakeStorage())
    _callback_env(
        monkeypatch,
        store,
        gateway_allow_insecure="1",
        feishu_operator_open_id="",
    )
    _patch_side_effects(monkeypatch)
    _store_push_hitl(store)

    await feishu_route._process_hitl_card(
        "approve", "hitl-1", "delivery:run-1", "ou_approver"
    )

    decision = read_decision(store, "hitl-1")
    assert decision is not None
    assert decision["decision"] == "approve"
    assert decision["gate"] == "insecure_operator_unverified"
