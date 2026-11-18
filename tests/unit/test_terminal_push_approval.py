"""A2 终端推送审批闸门的判定矩阵（plans/a2-terminal-identity-20261118.md）。

每一格对应契约里那张 3x3 表：白名单非空 / 空名单+insecure / 空名单+无 insecure，
乘以 approver 与 token 主人的关系。拒绝必须打印稳定原因码
（``self_approval`` / ``not_allowlisted``）；``get_authenticated_login()`` 失败
必须抛异常（flow 映射 INFRA_ERROR），不得伪装成拒绝或放行。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.cli import TerminalPushApprovalPort
from apps.code_review_pipeline.change.pipeline import ChangeApproval

RUN_ID = "run-1"


class InteractiveStdin:
    def isatty(self) -> bool:
        return True


class NonInteractiveStdin:
    def isatty(self) -> bool:
        return False


class StubGitHub:
    """get_authenticated_login 返回预设登录名的桩。"""

    def __init__(self, login: str) -> None:
        self._login = login

    async def get_authenticated_login(self) -> str:
        return self._login


class ExplodingGitHub:
    async def get_authenticated_login(self) -> str:
        raise RuntimeError("github api unreachable")


def _bundle() -> SimpleNamespace:
    return SimpleNamespace(
        branch="moa/fix-1",
        bundle_hash="bundle-1",
        patch_hash="patch-1",
        verify_exit_code=0,
        trace_id="trace-1",
    )


def _terminal_answers(monkeypatch: pytest.MonkeyPatch, approver: str) -> None:
    answers = iter([approver, "APPROVE"])
    monkeypatch.setattr("app.cli.sys.stdin", InteractiveStdin())
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))


def _allowlist(monkeypatch: pytest.MonkeyPatch, logins: tuple[str, ...]) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "github_approver_logins", logins)


def _request(
    monkeypatch: pytest.MonkeyPatch, github: object
) -> ChangeApproval | None:
    return asyncio.run(
        TerminalPushApprovalPort(github=github).request(
            run_id=RUN_ID, bundle=_bundle()
        )
    )


@pytest.fixture(autouse=True)
def _no_insecure_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GATEWAY_ALLOW_INSECURE", raising=False)


# ── 白名单非空 ────────────────────────────────────────────────────────────


def test_allowlist_member_other_than_operator_is_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """approver ∈ 白名单 且 ≠ token 主人 → 批准，gate=allowlist。"""
    _allowlist(monkeypatch, ("alice",))
    _terminal_answers(monkeypatch, "alice")

    approval = _request(monkeypatch, StubGitHub("bob"))

    assert approval is not None
    assert approval.gate == "allowlist"
    assert approval.channel == "terminal"
    assert approval.identity_verified is False
    assert approval.approver == "alice"
    assert approval.operator_login == "bob"


def test_allowlist_membership_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """approver ``Alice`` vs 白名单 ``alice``：大小写不敏感放行。"""
    _allowlist(monkeypatch, ("alice",))
    _terminal_answers(monkeypatch, "Alice")

    approval = _request(monkeypatch, StubGitHub("bob"))

    assert approval is not None
    assert approval.gate == "allowlist"


def test_self_approval_denied_even_when_operator_is_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """白名单模式下 token 主人（即使在白名单里）批准自己 → 拒绝 self_approval。"""
    _allowlist(monkeypatch, ("alice", "bob"))
    _terminal_answers(monkeypatch, "bob")

    approval = _request(monkeypatch, StubGitHub("bob"))

    assert approval is None
    assert "push approval denied: self_approval" in capsys.readouterr().out


def test_self_approval_reason_code_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """token 主人 ``ALICE`` vs approver ``alice``：比较大小写不敏感，拒绝。"""
    _allowlist(monkeypatch, ("alice", "carol"))
    _terminal_answers(monkeypatch, "alice")

    approval = _request(monkeypatch, StubGitHub("ALICE"))

    assert approval is None
    assert "push approval denied: self_approval" in capsys.readouterr().out


def test_non_member_denied_as_not_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """approver ∉ 白名单 → 拒绝 not_allowlisted。"""
    _allowlist(monkeypatch, ("alice",))
    _terminal_answers(monkeypatch, "eve")

    approval = _request(monkeypatch, StubGitHub("bob"))

    assert approval is None
    assert "push approval denied: not_allowlisted" in capsys.readouterr().out


# ── 空名单 + 显式 GATEWAY_ALLOW_INSECURE=1 ───────────────────────────────


def test_insecure_mode_approves_non_self_with_insecure_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空名单 + insecure + approver ≠ token 主人 → 批准，gate=insecure。"""
    _allowlist(monkeypatch, ())
    _terminal_answers(monkeypatch, "alice")
    monkeypatch.setenv("GATEWAY_ALLOW_INSECURE", "1")

    approval = _request(monkeypatch, StubGitHub("bob"))

    assert approval is not None
    assert approval.gate == "insecure"


def test_insecure_mode_marks_self_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空名单 + insecure + approver == token 主人 → 批准但标记
    gate=insecure_self_approval。"""
    _allowlist(monkeypatch, ())
    _terminal_answers(monkeypatch, "bob")
    monkeypatch.setenv("GATEWAY_ALLOW_INSECURE", "1")

    approval = _request(monkeypatch, StubGitHub("bob"))

    assert approval is not None
    assert approval.gate == "insecure_self_approval"
    assert approval.identity_verified is False


def test_insecure_mode_still_reports_self_approval_denial_when_env_is_falsey(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """insecure 只认真值（与 insecure_mode_enabled 同一套判定）：``0`` 不放行。"""
    _allowlist(monkeypatch, ())
    _terminal_answers(monkeypatch, "bob")
    monkeypatch.setenv("GATEWAY_ALLOW_INSECURE", "0")

    approval = _request(monkeypatch, StubGitHub("bob"))

    assert approval is None
    assert "push approval denied: self_approval" in capsys.readouterr().out


# ── 空名单 + 无 insecure（fail-closed）───────────────────────────────────


def test_empty_allowlist_without_insecure_denies_non_self_as_not_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """空白名单且无 insecure：拒绝 not_allowlisted，不能回到门全开。"""
    _allowlist(monkeypatch, ())
    _terminal_answers(monkeypatch, "alice")

    approval = _request(monkeypatch, StubGitHub("bob"))

    assert approval is None
    assert "push approval denied: not_allowlisted" in capsys.readouterr().out


def test_empty_allowlist_without_insecure_denies_self_approval(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """空白名单且无 insecure + approver == token 主人 → 拒绝 self_approval。"""
    _allowlist(monkeypatch, ())
    _terminal_answers(monkeypatch, "bob")

    approval = _request(monkeypatch, StubGitHub("bob"))

    assert approval is None
    assert "push approval denied: self_approval" in capsys.readouterr().out


# ── fail-closed 边界 ─────────────────────────────────────────────────────


def test_identity_read_failure_propagates_instead_of_denying_or_approving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_authenticated_login() 失败 → 异常上抛（flow 映射 INFRA_ERROR）。

    不得吞成 None（那是 REJECTED），更不得放行。
    """
    _allowlist(monkeypatch, ("alice",))
    _terminal_answers(monkeypatch, "alice")

    with pytest.raises(RuntimeError, match="github api unreachable"):
        _request(monkeypatch, ExplodingGitHub())


def test_non_tty_denial_does_not_touch_identity_api(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """非交互 stdin 仍是原样的 None → REJECTED，即便凭证已坏也不先炸。"""

    class BrokenGitHub:
        async def get_authenticated_login(self) -> str:
            raise AssertionError("identity API must not be called for non-TTY denial")

    monkeypatch.setattr("app.cli.sys.stdin", NonInteractiveStdin())

    approval = asyncio.run(
        TerminalPushApprovalPort(github=BrokenGitHub()).request(
            run_id=RUN_ID, bundle=_bundle()
        )
    )

    assert approval is None
    assert "non-interactive stdin" in capsys.readouterr().out
