"""Minimal e2e verification: use FastAPI TestClient + patched GitHub client."""
from __future__ import annotations

from typing import Any

import json
import pytest

from app.config import settings
from app.main import app
from apps.code_review_pipeline.rag.embeddings import EmbeddingError
from apps.code_review_pipeline.routing.github_client import GitHubClient
from tests.support import app_client


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
        },
        "repository": {
            "full_name": "shing26/moa-gateway",
        },
    }
    response = client.post("/webhook/github/review", json=payload)
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
        },
        "repository": {
            "full_name": "shing26/moa-gateway",
        },
    }
    response = client.post("/webhook/github/review", json=payload)
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
            assert response.status_code == 200
            body = response.json()
            assert body.get("status") not in ("reset", "suspended"), (
                f"{text!r} 被当成了指令（status={body.get('status')}）"
            )
