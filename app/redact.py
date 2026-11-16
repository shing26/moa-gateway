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
    # Authorization / Proxy-Authorization 头带 scheme 的值（Basic / token /
    # Digest / Bearer）：scheme 保留在上下文里，只换凭证。两种值形态：
    #   * 单 token（Basic 的 base64、token 的不透明串）——≥12 字符，且**至少含
    #     一个数字、大写字母或符号**（`+ / = . _ ~ -`）。纯小写字母的 ≥12 字符串
    #     按普通英文词处理（如 "Basic authentication failed"），否则
    #     "authentication"（14 个小写字母）会被当成凭证误杀。取舍：极少数纯小写
    #     不透明 token 会漏杀，且**没有别的规则兜底**（key-value 的键白名单不含
    #     authorization，Bearer 分支只覆盖 Bearer）——这是刻意的：ADR-035 的口径是
    #     宁漏勿误，审计记录被吞成占位符比漏掉一个低熵 token 更难补救。
    #   * Digest 的 `key="value", key2="value2"` 参数串——含 `=` 的整行剩余部分。
    # 注意 `(?i)` 下 `[A-Z]` 也会匹配小写，故"非小写字母"部分用 `(?-i:...)` 局部关闭。
    (
        "auth-header",
        re.compile(
            r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*"
            r"(?:basic|bearer|token|digest)\s+)"
            r"(?:(?=[A-Za-z0-9._~+/=-]{12,})"
            r"(?=[A-Za-z0-9._~+/=-]*(?-i:[0-9A-Z._~+/=-]))"
            r"[A-Za-z0-9._~+/=-]+"
            r"|\w+=[^\r\n]*)"
        ),
        1,
    ),
    # 不带 scheme 的 Authorization 头：保持原有语义不变（≥12 字符的密钥形）。
    # 单列一条，是为了让上面"必须含数字/大写/符号"的收紧**只作用于带 scheme 的
    # 值**，不误伤 "authorization: ffffffffffff..." 这类既有行为。
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
    # 覆盖三种常见书写形态：
    #   * `password = <value>`
    #   * `DB_PASSWORD=<value>` / `GITHUB_TOKEN=<value>`（带下划线前缀的变量名，
    #     此前 `\b` 锚定在 `_` 前失效而漏杀）
    #   * `{"password": "<value>"}`（JSON：字段名与分隔符之间、值外侧都有引号）
    # 保留组 = 字段名（含其收尾引号）+ 分隔符 + 值的开引号，值本身换占位符；
    # 值的收尾引号**不**吃掉，保证脱敏后仍是可读的合法片段。
    (
        "key-value",
        re.compile(
            r"(?i)\b((?:[A-Za-z0-9]+[_-])*"
            r"(?:api[_-]?key|secret|password|passwd|token|access[_-]?token)"
            r"[\"']?\s*[:=]\s*[\"']?)"
            r"([A-Za-z0-9._~/+=-]{12,})"
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
