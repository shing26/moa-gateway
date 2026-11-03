"""交付事件 webhook（issue #26）的单测。

被测的是**路由自己的判断**：开关、签名 fail-closed、action 过滤、状态机转移。
CI 轮询与合并执行由既有模块承担，它们的语义由各自的测试盯着，这里全部打桩。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import settings
from app.main import app
from apps.code_review_pipeline.merge_store import MergeStore
from apps.code_review_pipeline.routing.github_signature import sign_payload
from tests.support import app_client

_SECRET = "test-delivery-webhook-secret"


class RecordingNotifier:
    """替换 MergeNotifier：只记录调用，不发真飞书。"""

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []

    async def send_pr_opened_card(self, record: Any, *, title: str, url: str) -> bool:
        self.opened.append(
            {"task_key": record.task_key, "title": title, "url": url}
        )
        return True


@pytest.fixture(autouse=True)
def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "delivery_webhook_enabled", True)
    monkeypatch.setattr(settings, "github_webhook_secret", _SECRET)
    monkeypatch.setattr(settings, "gateway_allow_insecure", "")


def _wire(monkeypatch: pytest.MonkeyPatch, store: MergeStore, notifier: RecordingNotifier) -> None:
    from app.routes import github_delivery as route

    monkeypatch.setattr(route, "merge_store", store, raising=False)
    monkeypatch.setattr(route, "_notifier", lambda: notifier, raising=False)


def _post(
    client: Any,
    payload: dict[str, Any],
    *,
    secret: str | None = None,
    signed: bool = True,
) -> Any:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if signed:
        used = _SECRET if secret is None else secret
        headers["X-Hub-Signature-256"] = sign_payload(raw, used)
    return client.post("/webhook/github/delivery", content=raw, headers=headers)


def _pr_payload(action: str, pr_number: int, sha: str, **extra: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {"number": pr_number, "head": {"sha": sha}}
    pr.update(extra)
    return {
        "action": action,
        "number": pr_number,
        "pull_request": pr,
        "repository": {"full_name": "shing26/moa-gateway"},
    }


def test_disabled_flag_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭：没显式打开时这个端点不存在——默认行为改变必须显式开。"""
    monkeypatch.setattr(settings, "delivery_webhook_enabled", False)
    _wire(monkeypatch, MergeStore(), RecordingNotifier())
    client = app_client(app)
    response = _post(client, _pr_payload("opened", 1, "aaaa0000"))
    assert response.status_code == 404


def test_rejects_unsigned_and_bad_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """与 review webhook 同一道门：无签名 / 签错 / secret 未配都 fail-closed。"""
    _wire(monkeypatch, MergeStore(), RecordingNotifier())
    client = app_client(app)
    assert _post(client, _pr_payload("opened", 1, "aaaa0000"), signed=False).status_code == 401
    assert _post(client, _pr_payload("opened", 1, "aaaa0000"), secret="wrong").status_code == 401
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    assert _post(client, _pr_payload("opened", 1, "aaaa0000")).status_code == 401


def test_unhandled_action_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """labeled / edited 这类事件 200 ignored，不产生任何记录。"""
    store = MergeStore()
    notifier = RecordingNotifier()
    _wire(monkeypatch, store, notifier)
    client = app_client(app)
    for action in ("labeled", "edited", "assigned"):
        response = _post(client, _pr_payload(action, 7, "aaaa0000"))
        assert response.status_code == 200, action
        assert response.json().get("status") == "ignored", action
    assert store.list_open() == []
    assert notifier.opened == []


def test_opened_creates_watching_record_and_sends_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MergeStore()
    notifier = RecordingNotifier()
    _wire(monkeypatch, store, notifier)
    client = app_client(app)
    response = _post(
        client,
        _pr_payload(
            "opened",
            26,
            "bee00000face",
            title="feat: feishu delivery bridge",
            html_url="https://github.com/shing26/moa-gateway/pull/26",
        ),
    )
    assert response.status_code == 202
    assert response.json().get("state") == "watching"
    assert len(store.list_open()) == 1
    record = store.list_open()[0]
    assert record.repo == "shing26/moa-gateway"
    assert record.pr_number == 26
    assert record.head_sha == "bee00000face"
    assert record.state == "watching"
    # 通知卡无按钮（notification），但必须真的发了
    assert notifier.opened == [
        {
            "task_key": "shing26/moa-gateway#26@bee00000face",
            "title": "feat: feishu delivery bridge",
            "url": "https://github.com/shing26/moa-gateway/pull/26",
        }
    ]


def test_opened_redelivery_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 (repo, pr, sha) 重投 → 200 idempotent，记录数与通知数都不变。"""
    store = MergeStore()
    notifier = RecordingNotifier()
    _wire(monkeypatch, store, notifier)
    client = app_client(app)
    payload = _pr_payload("opened", 26, "bee00000face")
    assert _post(client, payload).status_code == 202
    second = _post(client, payload)
    assert second.status_code == 200
    assert second.json().get("status") == "idempotent"
    assert len(store.list_open()) == 1
    assert len(notifier.opened) == 1


def test_synchronize_supersedes_old_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """新 push = 新身份：旧记录 SUPERSEDED，新记录 WATCHING。

    这是 MergeRecord 身份三元组（"sha 变了就是另一条记录"）在事件入口的兑现：
    谁也不能再批准一个已经不是 head 的 sha。
    """
    store = MergeStore()
    notifier = RecordingNotifier()
    _wire(monkeypatch, store, notifier)
    client = app_client(app)
    assert _post(client, _pr_payload("opened", 26, "bee00000face")).status_code == 202
    response = _post(client, _pr_payload("synchronize", 26, "cafe1111dead"))
    assert response.status_code == 202
    assert response.json().get("superseded") == 1
    open_records = store.list_open()
    assert [r.head_sha for r in open_records] == ["cafe1111dead"]
    old = store.get(("shing26/moa-gateway", 26, "bee00000face"))
    assert old is not None and old.state == "superseded"


def test_closed_terminates_open_records_with_honest_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 在 GitHub 侧被关/被合 → 通道记录进终态，原因如实写进 failure_reason。

    状态机没有"通道外完成"这个动作——本切片不动治理状态机，用 FAIL（终态 +
    原因如实）是现有动作里唯一不撒谎的选择。
    """
    store = MergeStore()
    notifier = RecordingNotifier()
    _wire(monkeypatch, store, notifier)
    client = app_client(app)
    _post(client, _pr_payload("opened", 26, "bee00000face"))
    response = _post(
        client,
        _pr_payload("closed", 26, "bee00000face", merged=True),
    )
    assert response.status_code == 200
    assert response.json().get("terminated") == 1
    assert store.list_open() == []
    record = store.get(("shing26/moa-gateway", 26, "bee00000face"))
    assert record is not None and record.state == "failed"
    assert "合并" in record.failure_reason


def test_missing_identity_fields_return_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """payload 缺身份字段 → 400，不落一条没法轮询的空记录。"""
    store = MergeStore()
    notifier = RecordingNotifier()
    _wire(monkeypatch, store, notifier)
    client = app_client(app)
    raw = json.dumps(
        {"action": "opened", "repository": {"full_name": "shing26/moa-gateway"}},
        separators=(",", ":"),
    ).encode("utf-8")
    response = client.post(
        "/webhook/github/delivery",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign_payload(raw, _SECRET),
        },
    )
    assert response.status_code == 400
    assert store.list_open() == []
