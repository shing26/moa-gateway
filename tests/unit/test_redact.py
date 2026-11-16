"""输出侧脱敏（app.redact）的单元测试。

覆盖三类判据：

1. **该杀的杀**——各高置信度密钥形态各一条真实样例（长度取模式下限，
   防止阈值将来被调高后测试静默失效）；
2. **不该杀的不杀**——审计可读性是交付物：普通文本、低熵词、字段名说明
   文字必须原样通过；
3. **确定性**——同一输入重复调用产出同一输出（WAL 哈希链依赖此性质）。

每条期望输出都按 `_PATTERNS` 的**声明顺序**手工推演：先命中的模式先替换，
后面的模式在已替换的文本上继续跑。
"""

from __future__ import annotations

import pytest

from app.audit.models import AuditEntry
from app.redact import redact, redact_value

GHP = "ghp_" + "a" * 36
GH_PAT = "github_pat_" + "A" * 22 + "_" + "b" * 59

_D5 = "-" * 5
_PEM = (
    _D5 + "BEGIN RSA PRIVATE KEY" + _D5
    + "AAA"
    + _D5 + "END RSA PRIVATE KEY" + _D5
)

CASES = [
    pytest.param(
        f"token {GHP} end",
        "token [REDACTED:github-token] end",
        id="github-classic-pat",
    ),
    pytest.param(
        GH_PAT,
        "[REDACTED:github-token]",
        id="github-fine-grained-pat",
    ),
    pytest.param(
        "Actions token ghs_" + "c" * 36 + " leaked",
        "Actions token [REDACTED:github-token] leaked",
        id="github-actions-token",
    ),
    pytest.param(
        "sk-" + "d" * 24 + " end",
        "[REDACTED:openai-key] end",
        id="openai-key",
    ),
    pytest.param(
        "AKIA" + "B" * 16,
        "[REDACTED:aws-key]",
        id="aws-key",
    ),
    pytest.param(
        "Bearer " + "e" * 32,
        "Bearer [REDACTED:bearer-token]",
        id="bearer-keeps-prefix",
    ),
    pytest.param(
        "authorization: " + "f" * 30,
        "authorization: [REDACTED:auth-header]",
        id="auth-header",
    ),
    pytest.param(
        _PEM,
        "[REDACTED:private-key]",
        id="pem-block",
    ),
    pytest.param(
        f"fatal: unable to access 'https://owner:{GHP}@github.com/'",
        # github-token 先杀 token，url-credential 再杀 user:token 整体。
        "fatal: unable to access 'https://[REDACTED:url-credential]github.com/'",
        id="url-credential",
    ),
    pytest.param(
        "api_key = " + "h" * 20 + " end",
        "api_key = [REDACTED:key-value] end",
        id="generic-key-value",
    ),
    pytest.param(
        "Authorization: Basic c3ludGhldGljOnZhbHVlMTIzNA==",
        "Authorization: Basic [REDACTED:auth-header]",
        id="auth-header-basic-scheme-kept",
    ),
    pytest.param(
        "Authorization: token opaquetokenvalue1234",
        "Authorization: token [REDACTED:auth-header]",
        id="auth-header-token-scheme-kept",
    ),
    pytest.param(
        'Authorization: Digest username="Mufasa", '
        'realm="testrealm@host.com", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093"',
        "Authorization: Digest [REDACTED:auth-header]",
        id="auth-header-digest-scheme-kept",
    ),
    pytest.param(
        "DB_PASSWORD=envvalue123456",
        "DB_PASSWORD=[REDACTED:key-value]",
        id="prefixed-env-var-password",
    ),
    pytest.param(
        "GITHUB_TOKEN=envvalue123456",
        "GITHUB_TOKEN=[REDACTED:key-value]",
        id="prefixed-env-var-token",
    ),
    pytest.param(
        '{"password": "envvalue123456"}',
        '{"password": "[REDACTED:key-value]"}',
        id="json-key-value",
    ),
    pytest.param(
        'password = "envvalue123456"',
        'password = "[REDACTED:key-value]"',
        id="trailing-quote-preserved",
    ),
]

#: 评审用的 15 条普通审计文本：保守性判据——审计可读性是交付物，必须原样通过。
BENIGN_TEXTS = [
    "Authorization: missing",
    "Authorization: required",
    "Proxy-Authorization: absent",
    "token: rotated at 12:30",
    "secret: none configured",
    "access_token: absent",
    "password policy updated for all users",
    "Bearer of bad news: retry scheduled",
    "api_key: (not configured)",
    "GET https://github.com/owner/repo -> 200 OK",
    "oauth token refresh scheduled for midnight",
    "the token bucket refilled; retry 3/5",
    "secret scanning enabled on the repository",
    "password: required for production deploy",
    "API_KEY rotation is documented in runbook 4",
]


@pytest.mark.parametrize("text", BENIGN_TEXTS)
def test_benign_text_passes_through_unchanged(text: str) -> None:
    assert redact(text) == text


@pytest.mark.parametrize("dirty,expected", CASES)
def test_redact_each_shape(dirty: str, expected: str) -> None:
    assert redact(dirty) == expected


def test_clean_text_untouched() -> None:
    """说明性文字不被误杀：审计可读性是交付物。"""
    text = (
        "Retry 3/5 failed: connection timeout; "
        "Authorization policy requires review; "
        "the token bucket refilled; password policy updated."
    )
    assert redact(text) == text


def test_deterministic() -> None:
    dirty = GHP
    assert redact(dirty) == redact(dirty)


def test_redact_value_walks_containers() -> None:
    entry = {
        "a": GHP,
        "b": ["sk-" + "d" * 24, 3, ("AKIA" + "B" * 16,)],
        "c": {"d": "Authorization: " + "f" * 30},
    }
    out = redact_value(entry)
    assert out["a"] == "[REDACTED:github-token]"
    assert out["b"][0] == "[REDACTED:openai-key]"
    assert out["b"][1] == 3
    assert out["b"][2][0] == "[REDACTED:aws-key]"
    assert out["c"]["d"] == "Authorization: [REDACTED:auth-header]"


def test_audit_entry_redacts_agent_output() -> None:
    """审计唯一序列化出口必须落脱敏后的内容，且 agent_output_len 与落盘一致。"""
    secret = GHP
    entry = AuditEntry(
        trace_id="t1",
        session_id="s1",
        agent_name="implementer",
        agent_output=f"done; my {secret}",
        intent="delivery",
    )
    row = entry.to_audit_dict()
    assert secret not in row["agent_output"]
    assert row["agent_output"] == "done; my [REDACTED:github-token]"
    assert row["agent_output_len"] == len(row["agent_output"])
