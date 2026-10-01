"""Minimal e2e verification: use FastAPI TestClient + patched GitHub client."""
from __future__ import annotations

from typing import Any

import json
import pytest

from app.config import settings
from app.main import app
from apps.code_review_pipeline.rag.embeddings import EmbeddingError
from apps.code_review_pipeline.routing.github_client import GitHubClient
from apps.code_review_pipeline.routing.github_signature import sign_payload
from tests.support import app_client

_TEST_WEBHOOK_SECRET = "test-webhook-secret"


def _post_signed(client: Any, payload: dict[str, Any], *, secret: str | None = None) -> Any:
    """带真实 HMAC 签名投递事件。

    签名必须对**实际发出的字节**计算，所以这里手动序列化再带上签名头——如果改用
    ``client.post(..., json=payload, headers=...)``，httpx 内部序列化出的字节与这里
    算签名的字节可能不一致（分隔符/转义差异），测试会假失败。
    """
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    used = _TEST_WEBHOOK_SECRET if secret is None else secret
    return client.post(
        "/webhook/github/review",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign_payload(raw, used),
        },
    )


class DummyLLM:
    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return json.dumps({
            "findings": [],
            "summary": "e2e smoke",
            "recommendation": "approve",
            "stats": {},
        }, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _patch_github_and_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _mock_get_pr_files(self: GitHubClient, repo: Any, pr_number: int) -> list[dict[str, Any]]:
        return [
            {
                "filename": "README.md",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "changes": 1,
                "patch": "+e2e smoke test",
                "sha": "abc123",
            }
        ]

    async def _mock_get_pr(self: GitHubClient, repo: Any, pr_number: int) -> dict[str, Any]:
        return {
            "number": pr_number,
            "title": "e2e smoke",
            "state": "open",
            "user": {"login": "shing26"},
            "head": {"sha": "abc123"},
            "base": {"sha": "def456"},
            "html_url": "https://github.com/shing26/moa-gateway/pull/1",
            "diff_url": "https://github.com/shing26/moa-gateway/pull/1.diff",
            "labels": [],
            "requested_reviewers": [],
        }

    monkeypatch.setattr(GitHubClient, "get_pr_files", _mock_get_pr_files)
    monkeypatch.setattr(GitHubClient, "get_pr", _mock_get_pr)
    monkeypatch.setattr("apps.code_review_pipeline.routing.llm_factory.build_code_review_llm", lambda: DummyLLM())

    # 代码审查的 RAG 检索会调用外部 embedding 接口；单测里固定走“没有
    # embedding provider”的降级分支，避免测试依赖开发机的云端配置。
    async def _embeddings_disabled(texts: list[str], **_kwargs: Any) -> Any:
        raise EmbeddingError("test: embedding provider disabled")

    monkeypatch.setattr(
        "apps.code_review_pipeline.rag.retriever.generate_embeddings",
        _embeddings_disabled,
    )
    for module in (
        "apps.code_review_pipeline.agents.triage_agent",
        "apps.code_review_pipeline.agents.static_analysis_agent",
        "apps.code_review_pipeline.agents.semantic_review_agent",
        "apps.code_review_pipeline.agents.coverage_agent",
        "apps.code_review_pipeline.agents.report_agent",
    ):
        monkeypatch.setattr(f"{module}.build_code_review_llm", lambda: DummyLLM())
    monkeypatch.setattr(settings, "github_token", "test-token")
    # 端点自 2026-10-01 起 fail-closed 校验 X-Hub-Signature-256
    monkeypatch.setattr(settings, "github_webhook_secret", _TEST_WEBHOOK_SECRET)


def test_github_review_webhook_returns_accepted() -> None:
    client = app_client(app)
    payload = {
        "action": "opened",
        "number": 1,
        "pull_request": {
            "number": 1,
            "title": "e2e: verify webhook pipeline",
            "state": "open",
            "user": {"login": "shing26"},
            "head": {"sha": "abc1234"},
        },
        "repository": {
            "full_name": "shing26/moa-gateway",
        },
    }
    response = _post_signed(client, payload)
    print("STATUS:", response.status_code)
    print("BODY:", response.text[:500])
    assert response.status_code == 200
    body = response.json()
    assert body.get("status") == "accepted"
    assert "trace_id" in body
    assert "repo" in body
    assert "pr_number" in body
    assert "changed_files" in body
    assert "findings_by_severity" in body
    assert "need_human_review" in body
    # D1：trace_id 由 (repo, pr_number, head_sha) 决定
    assert body.get("task_key") == "shing26/moa-gateway#1@abc1234"
    assert body.get("trace_id") == "cr_shing26/moa-gateway#1@abc1234"


def test_github_review_webhook_degrades_without_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "github_token", "")
    client = app_client(app)
    payload = {
        "action": "opened",
        "number": 2,
        "pull_request": {
            "number": 2,
            "title": "e2e: no token",
            "state": "open",
            "user": {"login": "shing26"},
            "head": {"sha": "abc1234"},
        },
        "repository": {
            "full_name": "shing26/moa-gateway",
        },
    }
    response = _post_signed(client, payload)
    assert response.status_code == 200
    body = response.json()
    assert body.get("status") == "degraded"
    assert "GITHUB_TOKEN" in body.get("message", "")


def test_pr_message_in_review_mode_returns_graceful_not_500(monkeypatch) -> None:
    from app.deps import command_mode

    monkeypatch.setattr(settings, "github_token", "")
    command_mode.set("h2-sess", "review")
    try:
        with app_client(app) as client:
            response = client.post(
                "/webhook/feishu",
                json={"session_id": "h2-sess", "chat_id": "h2-chat", "text": "octocat/Hello-World#1"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body.get("status") == "ok"
        assert "GITHUB_TOKEN" in body.get("text", "")
    finally:
        command_mode.clear("h2-sess")


def test_webhook_cancel_returns_reset() -> None:
    with app_client(app) as client:
        response = client.post(
            "/webhook/feishu",
            json={"session_id": "ctrl-sess", "chat_id": "ctrl-chat", "text": "cancel"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body.get("status") == "reset"
    assert body.get("state") == "INIT"


def test_webhook_debug_returns_suspended() -> None:
    # 载荷改成**精确指令**（2026-09-29，探索性验收 D2）：此前 _map_event 用子串匹配，
    # 所以 "debug 错误" 这种词组也能触发。现在只有整条消息就是指令才算——本用例要验的
    # 是"敏感挂起这条路走得通"，不该依赖模糊匹配。
    with app_client(app) as client:
        response = client.post(
            "/webhook/feishu",
            json={"session_id": "debug-sess", "chat_id": "debug-chat", "text": "debug"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body.get("status") == "suspended"
    assert body.get("state") == "SUSPENDED"


def test_webhook_sentence_containing_command_words_is_not_a_command() -> None:
    """句子里出现"取消/报错"不该被当成指令（探索性验收 D2，2026-09-29）。

    此前是子串匹配："怎么取消订阅" 会 RESET 清空会话、"看看这个报错" 会敏感挂起——
    而挂起没有审批出口，用户只能再发一次 reset 才能继续。指令与对话必须分得开。
    """
    with app_client(app) as client:
        for text, session in (
            ("怎么取消订阅", "d2-cancel"),
            ("帮我看看这个报错", "d2-debug"),
        ):
            response = client.post(
                "/webhook/feishu",
                json={"session_id": session, "chat_id": session, "text": text},
            )
            body = response.json()
            # **只看 status 字段，不看 HTTP 码**：这一句最终是正常回答（本机配了 LLM）
            # 还是 agent 失败（CI 里没有凭据 → 路由返 500 error）与本用例无关。
            # 关键判据是"它没被当成指令"。
            assert body.get("status") not in ("reset", "suspended"), (
                f"{text!r} 被当成了指令（status={body.get('status')}）"
            )

# ── D1 前置修复（2026-10-01）────────────────────────────────────────


def test_webhook_rejects_unsigned_request() -> None:
    """D1 判据：无签名 → 401。端点此前零校验。"""
    client = app_client(app)
    response = client.post("/webhook/github/review", json={"action": "opened", "number": 1})
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_webhook_rejects_bad_signature() -> None:
    """签名算错 → 401。"""
    client = app_client(app)
    response = _post_signed(client, {"action": "opened", "number": 1}, secret="wrong-secret")
    assert response.status_code == 401


def test_webhook_fails_closed_when_secret_unset(monkeypatch) -> None:
    """没配 GITHUB_WEBHOOK_SECRET → 拒绝（fail-closed），不静默放行。

    与 AuthMiddleware / feishu_signature 同一套姿势。此处若 fail-open，
    "签名校验"就只是个可关闭的装饰。
    """
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    monkeypatch.setattr(settings, "gateway_allow_insecure", "")
    client = app_client(app)
    response = client.post("/webhook/github/review", json={"action": "opened", "number": 1})
    assert response.status_code == 401


def test_webhook_accepts_valid_signature() -> None:
    """签名正确 → 正常受理。"""
    client = app_client(app)
    response = _post_signed(client, {
        "action": "opened",
        "number": 3,
        "pull_request": {"number": 3, "head": {"sha": "deadbeefcafe"}},
        "repository": {"full_name": "shing26/moa-gateway"},
    })
    assert response.status_code == 200


def test_two_prs_in_same_repo_get_distinct_trace_ids() -> None:
    """D1 判据：同仓库两个 PR 产生两条独立记录。

    回归：此前 trace_id = f"cr_{repo}:{body['id']}"，而 pull_request 事件顶层没有
    id 字段 → 同一仓库所有 PR 共用一个主键，review_store 的
    ON CONFLICT (trace_id) DO UPDATE 让后一个 PR 覆盖前一个。
    """
    seen = set()
    for pr_number, sha in ((11, "aaaa1111"), (12, "bbbb2222")):
        body = _task_key_of(pr_number, sha)
        assert body not in seen, f"PR #{pr_number} 与前一个撞了同一个 task_key"
        seen.add(body)
    assert len(seen) == 2


def test_same_pr_redelivery_is_idempotent() -> None:
    """同一个 PR 重投 → 同一个 task_key（这正是幂等键要的行为）。"""
    assert _task_key_of(42, "abc1234") == _task_key_of(42, "abc1234")
    # head_sha 变了 = 代码变了 = 该重新审
    assert _task_key_of(42, "abc1234") != _task_key_of(42, "def5678")


def _task_key_of(pr_number: int, head_sha: str) -> str:
    from apps.code_review_pipeline.routing.github_review_route import build_task_key
    return build_task_key("shing26/moa-gateway", pr_number, head_sha)
