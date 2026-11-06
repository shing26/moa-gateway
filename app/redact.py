"""输出侧统一脱敏（对应输入侧的 ``app.agents.subprocess_env``）。

**为什么需要第二道防线**：``subprocess_env`` 用白名单管住了"凭证进不去"（输入侧），
但治理运行时会把 agent 输出、verify 命令回显、CI 日志等**不可信内容**写进三处
持久化位置：审计 WAL / ES（tamper-evident）、GitHub PR 正文与 commit message、
本地 trace 目录。前两处一旦写进密钥就是长期留痕，删都删不干净（git 历史与
哈希链的既定设计）。所以内容在**落盘或外发前**统一过一遍 ``redact()``——
这正是对齐 ADR-034 的后果：agent 以所有者 PAT 执行，PAT 形态（``github_pat_*``）
必须出现在脱敏清单里。

**单点原则**：脱敏在两处汇聚点生效——

* ``app.audit.models.AuditEntry.to_audit_dict()``（WAL 与 ES 的唯一序列化出口，
  哈希链覆盖的就是脱敏后落盘的内容）；
* ``change/pipeline.py`` 的建 commit / 开 PR 调用（外发到 GitHub 的唯一写口）。

**设计约束**：

* **确定性**——同一输入必须产出同一输出：WAL 哈希链要跨机器可复算，
  禁止任何随机或依赖环境的替换。
* **保守**——只匹配高置信度的密钥形态，宁漏勿误；误杀会把审计内容变得
  不可读，而审计的可读性本身是交付物（见 2026-09-28 "永远红"教训的同款逻辑）。
* **可检**——替换占位符统一为 ``[REDACTED:<kind>]``，运维可以 grep 统计
  哪些内容被动过，而不是默默吞掉。
"""

from __future__ import annotations

import re
from typing import Any

#: 占位符格式。grep 友好：``grep -o '\[REDACTED:[a-z-]*\]'`` 即可盘点。
_PLACEHOLDER = "[REDACTED:{kind}]"

# 高置信度密钥形态。阈值取"形态最短合法长度"：GitHub 经典 PAT 为 36 字符、
# fine-grained PAT 的尾部段更长；sk- 与 AKIA 同理。过短会把普通词误杀。
# keep_group：命中后保留第几个捕获组（上下文可读性），None 表示整段替换。
_PATTERNS: tuple[tuple[str, re.Pattern[str], int | None], ...] = (
    (
        "github-token",
        re.compile(
            r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"
            r"|\bgithub_pat_[A-Za-z0-9_]{40,}\b"
        ),
        None,
    ),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), None),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), None),
    # "Bearer <token>"：只吃 token 部分，保留 Bearer 前缀让上下文可读。
    (
        "bearer-token",
        re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}\b"),
        1,
    ),
    # Authorization / Proxy-Authorization 头的值。要求 ≥12 字符的"密钥形"
    # 字符，避免把 "Authorization: missing" 这类说明文字杀掉。
    (
        "auth-header",
        re.compile(
            r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)"
            r"([A-Za-z0-9._~+/=-]{12,})"
        ),
        1,
    ),
    # PEM 私钥整块替换（DOTALL，跨行）。
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        None,
    ),
    # URL 内嵌凭证（git 报错信息常见形态：https://user:token@host/...）。
    ("url-credential", re.compile(r"(?i)\b(https?://)[^\s/:@]+:[^\s/@]+@"), 1),
    # 通用的 key=value / key: value 形态。字段名白名单 + 值长 ≥12 的双门槛，
    # 仍然是最容易误杀的一条，所以放在最后、置信度最低。
    # 保留组 = 字段名 + 分隔符（合为一组），值本身换占位符。
    (
        "key-value",
        re.compile(
            r"(?i)\b((?:api[_-]?key|secret|password|passwd|token|access[_-]?token)\b"
            r"\s*[:=]\s*[\"']?)([A-Za-z0-9._~/+=-]{12,})[\"']?"
        ),
        1,
    ),
)


def redact(text: str) -> str:
    """对一段**不可信内容**做输出侧脱敏。确定性：同一输入永远同一输出。"""
    for kind, pattern, keep_group in _PATTERNS:
        if keep_group is None:

            def _sub(m: re.Match[str], kind: str = kind) -> str:
                return _PLACEHOLDER.format(kind=kind)

        else:

            def _sub(m: re.Match[str], kind: str = kind, keep: int = keep_group) -> str:
                # 密钥永远是模式的最后一段：保留 keep 组（上下文），后面整体换成占位符。
                return m.group(keep) + _PLACEHOLDER.format(kind=kind)

        text = pattern.sub(_sub, text)
    return text


def redact_value(value: Any) -> Any:
    """递归脱敏：dict/list/tuple 里的字符串值逐个过 ``redact()``。

    只变换**值**，不动键名（审计字段覆盖测试依赖键集合稳定），不返回新键。
    非 string 容器原样重建，其他类型原样返回。
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v) for v in value)
    return value


__all__ = ["redact", "redact_value"]
