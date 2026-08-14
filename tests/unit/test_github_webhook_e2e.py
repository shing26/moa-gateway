"""Minimal e2e verification: use FastAPI TestClient + patched GitHub client."""
from __future__ import annotations

from typing import Any

import json
import pytest
from fastapi.testclient import TestClient

from app.main import app
from apps.code_review_pipeline.routing.github_client import GitHubClient


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
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")


def test_github_review_webhook_returns_accepted() -> None:
    client = TestClient(app)
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
