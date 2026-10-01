"""GitHub webhook 的 ``X-Hub-Signature-256`` 校验（fail-closed）。

策略与 ``app/channels/feishu_signature.py``、``app/middleware/auth.py`` 同一套，
三态：

- **配了 secret**：必须带合法签名，常量时间比对；取不到或格式错一律拒。
- **没配 secret**：仅当显式 ``GATEWAY_ALLOW_INSECURE=1`` 才放行，否则拒绝。

**校验的是原始字节，不是解析后的 JSON。** 这一点是签名验证的核心：HMAC 覆盖的是
GitHub 发出的请求体，任何重新序列化（key 顺序、空白、Unicode 转义）都会让摘要对不上。
所以本函数接 ``bytes``，调用方必须拿 ``await request.body()``，不能拿
``await request.json()`` 再 dump 回去。

为什么现在才加（2026-10-01）：此前本项目对该端点**零校验**。只读审查时后果有限
（顶多浪费几次 LLM 调用），但主线要往这条链路加"写回 GitHub 评论"——那意味着任何
能访问该端点的人都能让流水线替我们往**任意仓库**发评论。写回之前必须先装门锁。
"""

from __future__ import annotations

import hashlib
import hmac

_SIGNATURE_PREFIX = "sha256="


def sign_payload(payload: bytes, secret: str) -> str:
    """按 GitHub 的格式计算签名。CLI 回放与单测用同一条路径，避免两套算法。"""
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def verify_signature(
    payload: bytes,
    signature: str | None,
    secret: str,
    *,
    allow_insecure: bool = False,
) -> tuple[bool, str]:
    """校验 webhook 请求体。返回 ``(是否通过, 拒绝原因)``。

    ``allow_insecure`` 由调用方从 ``GATEWAY_ALLOW_INSECURE`` 派生。
    拒绝原因进日志，响应体只回笼统的 ``unauthorized``——把"你缺签名头"还是
    "你的签名算错了"分开告诉调用方，等于给攻击者一个免费的区分oracle。
    """
    if not secret:
        if allow_insecure:
            return True, ""
        return False, "auth_not_configured"
    if not signature:
        return False, "signature_missing"
    if not signature.startswith(_SIGNATURE_PREFIX):
        return False, "signature_malformed"
    expected = sign_payload(payload, secret)
    if not hmac.compare_digest(signature, expected):
        return False, "signature_mismatch"
    return True, ""


__all__ = ["sign_payload", "verify_signature"]
