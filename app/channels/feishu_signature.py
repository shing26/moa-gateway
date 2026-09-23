"""飞书回调的 token 校验（fail-closed，策略与 ``AuthMiddleware`` 同一套）。

回归（2026-09-23 实测）：此前本函数只在**顶层** ``body["token"]`` 里找凭据，而
**v2 事件把 verification token 放在 ``header.token``**（``parse_feishu_event`` 也是按
``header.event_id`` 判 v2 的）。于是"配了 ``FEISHU_VERIFICATION_TOKEN`` 却从未真正
比对过"——v2 请求永远走不到比较分支，直接 ``return True``。这与本项目反复出现的
"配了没生效"是同一类问题，只是这次配的是**门锁**。

现在：
- 配了 token：顶层（v1）或 ``header.token``（v2）必须有，且常量时间相等；
  **取不到即未通过**（不再静默放行）。
- 没配 token：仅当显式允许 insecure（``GATEWAY_ALLOW_INSECURE``，本地调试）才放行，
  否则拒绝——与 ``/webhook``、``/dashboard`` 的既有策略一致。

未实现：飞书加密模式（``FEISHU_ENCRYPT_KEY`` / ``X-Lark-Signature`` 的 HMAC 与时间戳
防重放）。本函数只看 verification token；加密模式未做，见 README 已知边界。
"""

from __future__ import annotations

import hmac
from typing import Any

# v1 把 token 放顶层，v2 放 header.token
_TOKEN_PATHS: tuple[tuple[str, ...], ...] = (("token",), ("header", "token"))


def extract_token(body: Any) -> str:
    """从回调体里取出平台下发的 verification token（v1 顶层 / v2 ``header.token``）。"""
    for path in _TOKEN_PATHS:
        node: Any = body
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str) and node:
            return node
    return ""


def verify_verification_token(
    body: Any,
    token: str,
    *,
    allow_insecure: bool = False,
) -> bool:
    """校验回调体是否来自平台。

    ``allow_insecure`` 由调用方从 ``GATEWAY_ALLOW_INSECURE`` 派生
    （``insecure_mode_enabled`` 在 ``app/middleware/auth.py``，三个受保护端点共用同一判定）。
    """
    if not isinstance(body, dict):
        return False
    if not token:
        # 未配置凭据：与 AuthMiddleware 一致 fail-closed，只在显式 insecure 时放行
        return bool(allow_insecure)
    supplied = extract_token(body)
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))


__all__ = ["extract_token", "verify_verification_token"]
