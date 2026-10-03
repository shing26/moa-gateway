"""GitHub 通道的代理策略：默认直连，可显式打开系统代理。"""
from __future__ import annotations

import pytest

from apps.code_review_pipeline.routing.github_client import GitHubClient


def test_defaults_to_direct_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """不设开关时直连——本机代理做 TLS 拦截会让 GitHub 调用死在证书校验上。"""
    monkeypatch.delenv("CODE_REVIEW_GITHUB_TRUST_ENV", raising=False)
    assert GitHubClient(token="t")._client.trust_env is False  # noqa: SLF001


@pytest.mark.parametrize("raw", ["1", "true", "YES"])
def test_env_switch_reopens_proxy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("CODE_REVIEW_GITHUB_TRUST_ENV", raw)
    assert GitHubClient(token="t")._client.trust_env is True  # noqa: SLF001


def test_explicit_argument_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_GITHUB_TRUST_ENV", "1")
    client = GitHubClient(token="t", trust_env=False)
    assert client._client.trust_env is False  # noqa: SLF001
