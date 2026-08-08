from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest

from apps.code_review_pipeline.rag.ingestion.historical_prs import (
    _chunk_pr,
    _filter_modules,
    _module_matches,
    _parse_repo,
    _truncate,
)
from apps.code_review_pipeline.routing.github_client import GitHubRepo
from apps.code_review_pipeline.rag.ingestion.historical_prs import HistoricalPR


# ── helpers ─────────────────────────────────────────────────────────────

def _make_pr(**overrides):
    default_repo = GitHubRepo(owner="org", name="repo")
    data = {
        "repo": default_repo,
        "pr_number": 1,
        "title": "Add redis lock timeout",
        "author": "alice",
        "merged_at": "2026-07-01T00:00:00Z",
        "html_url": "https://github.com/org/repo/pull/1",
        "diff": "diff --git a/auth/redis_lock.py ...",
        "description": "Fix deadlock by adding timeout.",
        "modules": ("auth",),
        "approvers": ("bob", "charlie"),
    }
    data.update(overrides)
    return HistoricalPR(**data)


# ── parse / filter ──────────────────────────────────────────────────────

def test_parse_repo_valid() -> None:
    repo = _parse_repo("org/repo")
    assert repo.owner == "org"
    assert repo.name == "repo"


def test_parse_repo_invalid() -> None:
    with pytest.raises(ValueError):
        _parse_repo("invalid")


def test_module_matches_case_insensitive() -> None:
    assert _module_matches("auth/views.py", ("auth",)) is True
    assert _module_matches("payment/processor.py", ("auth", "payment")) is True
    assert _module_matches("billing/x.py", ("auth", "payment")) is False


def test_filter_modules_returns_matched() -> None:
    files = [
        {"filename": "auth/login.py"},
        {"filename": "payment/charge.py"},
        {"filename": "core/base.py"},
    ]
    assert _filter_modules(files, ("auth", "payment")) == ("auth", "payment")


def test_filter_modules_no_match() -> None:
    files = [{"filename": "core/base.py"}]
    assert _filter_modules(files, ("auth", "payment")) == ()


# ── chunking ────────────────────────────────────────────────────────────

def test_truncate_short_text_unchanged() -> None:
    assert _truncate("hello", 10) == "hello"


def test_truncate_long_text_adds_ellipsis() -> None:
    text = "x" * 20
    assert _truncate(text, 10) == "xxxxxxx..."


def test_chunk_pr_summary_and_short_diff() -> None:
    pr = _make_pr(diff="small diff")
    docs = _chunk_pr(pr)
    assert len(docs) == 2
    assert docs[0].metadata["chunk_type"] == "summary"
    assert docs[1].metadata["chunk_type"] == "diff"
    assert docs[1].source_id.endswith("-diff")


def test_chunk_pr_long_diff_is_chunked() -> None:
    long_diff = "x" * 5000
    pr = _make_pr(diff=long_diff)
    docs = _chunk_pr(pr)
    diff_docs = [doc for doc in docs if doc.metadata.get("chunk_type") == "diff"]
    assert len(diff_docs) > 1
    assert all(doc.metadata.get("chunk_total") == len(diff_docs) for doc in diff_docs)


def test_chunk_pr_empty_diff_only_summary() -> None:
    pr = _make_pr(diff="", description="")
    docs = _chunk_pr(pr)
    assert len(docs) == 1
    assert docs[0].metadata["chunk_type"] == "summary"


def test_chunk_pr_metadata_includes_modules() -> None:
    pr = _make_pr(modules=("auth", "payment"))
    docs = _chunk_pr(pr)
    for doc in docs:
        assert doc.metadata.get("modules") == ("auth", "payment")
