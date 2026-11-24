from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.channels.base import ChannelMessage
from app.deps import pipeline
from app.main import app
from app.pipeline import PipelineResult
import app.routes.feishu as feishu_route
from apps.code_review_pipeline.merge_state import MergeAction, MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.routing.github_client import MergeOutcome


class FakeFeishuAdapter:
    def __init__(self):
        self.sent: list[ChannelMessage] = []

    async def send(self, message: ChannelMessage) -> bool:
        self.sent.append(message)
        return True


def _event(text: str, chat_id: str = "c-route-test") -> dict:
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "event_id": "m-route-test"},
        "event": {
            "message": {
                "message_id": "m-route-test",
                "chat_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            "sender": {"sender_id": {"user_id": "u-route-test"}},
        },
    }


def _fake_get_adapter(adapter):
    async def _get():
        return adapter

    return _get


def _clear_seen():
    feishu_route._seen_events.clear()


def test_card_action_retry_is_answered_with_empty_object(monkeypatch) -> None:
    """卡片回调一律回**空对象**，重投也不例外（2026-09-29 修 200341）。

    此前重投命中 dedup 分支、返回 `{"msg": "duplicate"}` —— 而卡片回调返回业务结构会被
    客户端当成"卡片更新"去解析、解析失败即报 200341。重投恰恰是"第一次响应偏慢"的
    后果，所以这条路径在真实点击里很容易被走到（本机实测就是先看到 200341）。
    """
    _clear_seen()
    adapter = FakeFeishuAdapter()
    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(adapter))

    body = {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger", "event_id": "card-retry-1"},
        "event": {
            "operator": {"open_id": "ou_someone"},
            "action": {"value": {"action": "approve", "trace_id": "t-retry"}},
            "context": {"open_chat_id": "oc-x", "open_message_id": "om-x"},
        },
    }
    with TestClient(app) as client:
        first = client.post("/feishu/event", json=body)
        second = client.post("/feishu/event", json=body)

    assert first.status_code == 200
    assert first.json() == {}, "受理必须回空对象"
    assert second.status_code == 200
    assert second.json() == {}, "重投也必须是空对象，否则客户端报 200341"


def test_encrypted_event_is_rejected_loudly() -> None:
    """加密模式未实现，收到加密体必须**明确报错**而不是静默丢弃。

    此前它落到 "ignored" 分支：配了加密后收不到任何事件、日志里也看不出原因（2026-09-23 修）。
    """
    with TestClient(app) as client:
        res = client.post("/feishu/event", json={"encrypt": "BASE64BLOB"})

    assert res.status_code == 400
    assert res.json()["error"] == "encrypted_events_unsupported"


@pytest.mark.asyncio
async def test_hitl_click_by_unauthorized_operator_is_refused_and_not_consumed(monkeypatch) -> None:
    """审批人白名单（非空即强制）：不在名单里的点击被拒，且**挂起记录不被消耗**。

    不消耗是关键——否则一个没有权限的人点一下就把待审批记录点没了，别人再也批不了。
    """
    from app.config import settings
    from app.engine import HitlRequest

    monkeypatch.setattr(settings, "hitl_approver_ids", ("ou_allowed",))
    store = feishu_route.engine.session_store
    store.store_hitl("authz-sess", HitlRequest(
        session_id="authz-sess", trace_id="authz-trace", agent_output="out",
        intent="coding", agent_name="coder", channel="feishu", target="chat",
    ))

    try:
        await feishu_route._process_hitl_card("approve", "authz-trace", "authz-sess", "ou_outsider")
        assert store.get_hitl("authz-trace") is not None, "无权限的点击不能消耗挂起记录"
    finally:
        store.remove_hitl("authz-trace")


@pytest.mark.asyncio
async def test_hitl_click_by_allowed_operator_proceeds(monkeypatch) -> None:
    from app.config import settings
    from app.engine import HitlRequest

    monkeypatch.setattr(settings, "hitl_approver_ids", ("ou_allowed",))
    store = feishu_route.engine.session_store
    store.store_hitl("authz-ok", HitlRequest(
        session_id="authz-ok", trace_id="authz-ok-trace", agent_output="out",
        intent="coding", agent_name="coder", channel="feishu", target="chat",
    ))
    # 会话推到 SUSPENDED，好让 decide_hitl 不是"已失效"分支
    from app.fsm.state_machine import Event as FsmEvent
    from app.models.events import MoAEvent

    await feishu_route.engine.handle_event(MoAEvent(
        trace_id="authz-ok-trace", event=FsmEvent.MESSAGE_RECEIVED,
        session_id="authz-ok", text="", context={},
    ))
    await feishu_route.engine.handle_event(MoAEvent(
        trace_id="authz-ok-trace", event=FsmEvent.NEEDS_HUMAN,
        session_id="authz-ok", text="", context={},
    ))

    try:
        await feishu_route._process_hitl_card("approve", "authz-ok-trace", "authz-ok", "ou_allowed")
        assert store.get_hitl("authz-ok-trace") is None, "有权限的点击应当消耗记录"
    finally:
        store.remove_hitl("authz-ok-trace")


# 2026-09-23 起不再 skip：conftest 已隔离 FEISHU_VERIFICATION_TOKEN 并显式开 insecure，
# 这条用例本来就不需要真实环境（自己 monkeypatch 了 adapter）。
def test_feishu_command_sends_reply(monkeypatch) -> None:
    _clear_seen()
    adapter = FakeFeishuAdapter()
    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(adapter))

    with TestClient(app) as client:
        res = client.post("/feishu/event", json=_event("/coding"))
        assert res.status_code == 200
        assert res.json() == {"msg": "ok"}

    assert len(adapter.sent) == 1
    msg = adapter.sent[0]
    assert msg.channel == "feishu"
    assert msg.target == "c-route-test"
    assert "编程" in msg.text
    assert msg.trace_id


# 2026-09-23 起不再 skip：conftest 已隔离 FEISHU_VERIFICATION_TOKEN 并显式开 insecure，
# 这条用例本来就不需要真实环境（自己 monkeypatch 了 adapter）。
def test_feishu_pipeline_review_sends_pending_hint(monkeypatch) -> None:
    _clear_seen()
    adapter = FakeFeishuAdapter()

    async def fake_run(event, *, channel, target, request=None):
        return PipelineResult(
            trace_id=event.trace_id, state="SUSPENDED", intent="coding",
            text="Output requires human approval before delivery",
            status="pending_review", need_human_review=True,
        )

    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(adapter))
    monkeypatch.setattr(pipeline, "run", fake_run)

    with TestClient(app) as client:
        res = client.post("/feishu/event", json=_event("帮我执行代码"))
        assert res.status_code == 200
        assert res.json() == {"msg": "ok"}

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "输出需要人工审批"


# 2026-09-23 起不再 skip：conftest 已隔离 FEISHU_VERIFICATION_TOKEN 并显式开 insecure，
# 这条用例本来就不需要真实环境（自己 monkeypatch 了 adapter）。
def test_feishu_pipeline_error_sends_friendly_text(monkeypatch) -> None:
    _clear_seen()
    adapter = FakeFeishuAdapter()

    async def fake_run(event, *, channel, target, request=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(adapter))
    monkeypatch.setattr(pipeline, "run", fake_run)

    with TestClient(app) as client:
        res = client.post("/feishu/event", json=_event("hello"))
        assert res.status_code == 200
        assert res.json() == {"msg": "ok"}

    assert len(adapter.sent) == 1
    assert "出错了" in adapter.sent[0].text


# 2026-09-23 起不再 skip：conftest 已隔离 FEISHU_VERIFICATION_TOKEN 并显式开 insecure，
# 这条用例本来就不需要真实环境（自己 monkeypatch 了 adapter）。
def test_feishu_pipeline_blocked_sends_reason(monkeypatch) -> None:
    _clear_seen()
    adapter = FakeFeishuAdapter()

    async def fake_run(event, *, channel, target, request=None):
        return PipelineResult(
            trace_id=event.trace_id, state="ROUTED", intent="coding",
            text="resource 'guard' requires admin role", status="blocked",
        )

    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(adapter))
    monkeypatch.setattr(pipeline, "run", fake_run)

    with TestClient(app) as client:
        res = client.post("/feishu/event", json=_event("hello"))
        assert res.status_code == 200
        assert res.json() == {"msg": "ok"}

    assert len(adapter.sent) == 1
    assert "admin role" in adapter.sent[0].text


# 2026-09-23 起不再 skip：conftest 已隔离 FEISHU_VERIFICATION_TOKEN 并显式开 insecure，
# 这条用例本来就不需要真实环境（自己 monkeypatch 了 adapter）。
def test_feishu_no_adapter_still_returns_ok(monkeypatch) -> None:
    _clear_seen()
    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(None))

    with TestClient(app) as client:
        res = client.post("/feishu/event", json=_event("hello"))
        assert res.status_code == 200
        assert res.json() == {"msg": "ok"}


class _RecordingMergeNotifier:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.raises = raises

    async def send_resolved_card(
        self, record, outcome: str, detail: str = ""
    ) -> bool:
        self.calls.append((record.task_key, outcome, detail))
        if self.raises:
            raise RuntimeError("feishu down")
        return True


class _FakeMergeExecutor:
    def __init__(self, outcome: MergeOutcome, on_execute) -> None:
        self._outcome = outcome
        self._on_execute = on_execute

    async def execute(self, hitl_id: str, action: str, operator: str) -> MergeOutcome:
        self._on_execute(hitl_id, action, operator)
        return self._outcome


def _merge_record(
    *, card_message_id: str = "om_card_1"
) -> MergeRecord:
    return MergeRecord(
        repo="owner/repo",
        pr_number=42,
        head_sha="abc123",
        state=MergeState.AWAITING_APPROVAL.value,
        card_message_id=card_message_id,
    )


async def _drive_merge_approval(
    monkeypatch,
    outcome: MergeOutcome,
    notifier: _RecordingMergeNotifier,
    *,
    record: MergeRecord | None = None,
    after_execute=None,
    hitl_id: str | None = None,
) -> tuple:
    store = MergeStore()
    if record is not None:
        store.save(record)

    def _apply_outcome(_hitl_id: str, _action: str, operator: str) -> None:
        if record is None:
            return
        if outcome.merged:
            store.transition(
                record.identity,
                MergeAction.APPROVE,
                at="t",
                approver=operator,
            )
            store.transition(
                record.identity,
                MergeAction.COMPLETE,
                at="t",
                merged_sha=outcome.sha,
            )
        elif outcome.status == "rejected":
            store.transition(
                record.identity,
                MergeAction.REJECT,
                at="t",
                approver=operator,
            )
        else:
            store.transition(
                record.identity,
                MergeAction.FAIL,
                at="t",
                failure_reason=outcome.message,
            )

    async def _noop_send(*_args, **_kwargs) -> None:
        return None

    async def _noop_audit(**_kwargs) -> None:
        return None

    monkeypatch.setattr(feishu_route, "merge_store", store)
    monkeypatch.setattr(feishu_route, "build_github_client", lambda: object())
    monkeypatch.setattr(
        feishu_route,
        "MergeExecutor",
        lambda *_args, **_kwargs: _FakeMergeExecutor(
            outcome, after_execute or _apply_outcome
        ),
    )
    monkeypatch.setattr(feishu_route, "_merge_notifier", lambda: notifier)
    monkeypatch.setattr(feishu_route, "_safe_send", _noop_send)
    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_human_decision", _noop_audit
    )

    task_key = hitl_id or (
        record.task_key if record is not None else "owner/repo#42@abc123"
    )
    response = await feishu_route._handle_merge_approval(
        hitl_id=task_key,
        action="approve",
        operator_id="ou_operator",
        session_id="oc_chat",
        trace_id=task_key,
    )
    return response, store


@pytest.mark.asyncio
async def test_merge_approval_merged_resolves_card(monkeypatch) -> None:
    """F1：合并成功后按最新记录调用一次 merged 改卡。"""
    record = _merge_record()
    notifier = _RecordingMergeNotifier()
    outcome = MergeOutcome(merged=True, status="merged", sha="deadbeefcafe")

    response, _store = await _drive_merge_approval(
        monkeypatch, outcome, notifier, record=record
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "trace_id": record.task_key,
        "status": "merged",
        "sha": "deadbeefcafe",
    }
    assert notifier.calls == [(record.task_key, "merged", "deadbeefcafe")]


@pytest.mark.asyncio
async def test_merge_approval_rejected_resolves_card(monkeypatch) -> None:
    """F2：拒绝后调用 rejected 改卡，detail 是操作者身份。"""
    record = _merge_record()
    notifier = _RecordingMergeNotifier()
    outcome = MergeOutcome(merged=False, status="rejected", message="已拒绝")

    response, _store = await _drive_merge_approval(
        monkeypatch, outcome, notifier, record=record
    )

    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "rejected"
    assert notifier.calls == [(record.task_key, "rejected", "ou_operator")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("error", "github read failed"),
        ("not_mergeable", "merge conflict on main"),
    ],
)
async def test_merge_approval_failure_resolves_card(
    monkeypatch, status: str, message: str
) -> None:
    """F3：error / not_mergeable 走 failed 改卡，detail 是原始失败原因。"""
    record = _merge_record()
    notifier = _RecordingMergeNotifier()
    outcome = MergeOutcome(merged=False, status=status, message=message)

    response, _store = await _drive_merge_approval(
        monkeypatch, outcome, notifier, record=record
    )

    assert response.status_code == 200
    assert json.loads(response.body)["status"] == status
    assert notifier.calls == [(record.task_key, "failed", message)]


@pytest.mark.asyncio
async def test_merge_approval_missing_record_still_returns_response(
    monkeypatch,
) -> None:
    """F4：执行后记录不存在只跳过改卡，审批响应不受影响。"""
    notifier = _RecordingMergeNotifier()
    outcome = MergeOutcome(merged=True, status="merged", sha="deadbeefcafe")

    response, _store = await _drive_merge_approval(
        monkeypatch,
        outcome,
        notifier,
        record=None,
        after_execute=lambda *_args: None,
    )

    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "merged"
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_merge_approval_resolve_exception_does_not_change_response(
    monkeypatch,
) -> None:
    """F4：改卡抛异常只告警，不 500、不改合并结论。"""
    record = _merge_record()
    notifier = _RecordingMergeNotifier(raises=True)
    outcome = MergeOutcome(merged=True, status="merged", sha="deadbeefcafe")

    response, _store = await _drive_merge_approval(
        monkeypatch, outcome, notifier, record=record
    )

    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "merged"
    assert notifier.calls == [(record.task_key, "merged", "deadbeefcafe")]


def test_parse_merge_identity_uses_last_separators() -> None:
    """F3：只按最后一个 @ 与最后一个 # 切分。"""
    assert feishu_route._parse_merge_identity(
        "owner/repo#old#42@abc123"
    ) == ("owner/repo#old", 42, "abc123")
    assert feishu_route._parse_merge_identity("not-a-task-key") is None


def test_parse_merge_identity_unicode_digit_only_warns(caplog) -> None:
    """P2：Unicode 上标数字不能让 int() 抛异常，只告警并返回 None。"""
    with caplog.at_level("WARNING", logger="moa.routes.feishu"):
        assert feishu_route._parse_merge_identity("owner/repo#²@abc123") is None
    assert "cannot parse merge identity" in caplog.text


@pytest.mark.asyncio
async def test_merge_approval_unparseable_hitl_id_skips_card(
    monkeypatch,
) -> None:
    """F3：hitl_id 解析失败时跳过改卡，响应照常。"""
    notifier = _RecordingMergeNotifier()
    outcome = MergeOutcome(merged=True, status="merged", sha="deadbeefcafe")

    response, _store = await _drive_merge_approval(
        monkeypatch,
        outcome,
        notifier,
        record=None,
        after_execute=lambda *_args: None,
        hitl_id="not-a-task-key",
    )

    assert response.status_code == 200
    assert notifier.calls == []
