"""统一错误契约（M1）：错误码枚举 + MoaError 基类 + HTTP 状态映射。

此前错误是三套词汇表并存：``PipelineResult.status`` 的字符串值、路由层
各自硬编码的 ``{"error": "<字面量>"}``、以及两个孤立的自定义异常。本模块
把"错误码"收进单一枚举：双引擎的错误出口产出 code，路由层按
``http_status_for`` 统一映射状态码，``main.py`` 全局兜底识别 ``MoaError``。

新增错误码只改 ``ErrorCode`` 与 ``HTTP_STATUS`` 两处；既有字面量的取值
保持不变——webhook/dashboard 的响应体已是外部契约，本模块只收敛来源，
不改变线上形状。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    INTERNAL_ERROR = "internal_error"
    AGENT_FAILED = "agent_failed"
    AGENT_NOT_REGISTERED = "agent_not_registered"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    RATE_LIMITED = "rate_limited"
    INVALID_CALLBACK_PAYLOAD = "invalid_callback_payload"
    HITL_REQUEST_NOT_FOUND = "hitl_request_not_found"
    UNKNOWN_ACTION = "unknown_action"
    INVALID_JSON = "invalid_json"
    UNAUTHORIZED = "unauthorized"
    VALIDATION_ERROR = "validation_error"
    BUDGET_EXCEEDED = "budget_exceeded"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.BUDGET_EXCEEDED: 429,
    ErrorCode.INVALID_CALLBACK_PAYLOAD: 400,
    ErrorCode.INVALID_JSON: 400,
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.UNKNOWN_ACTION: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.HITL_REQUEST_NOT_FOUND: 404,
    ErrorCode.AGENT_FAILED: 500,
    ErrorCode.AGENT_NOT_REGISTERED: 500,
    ErrorCode.INVALID_STATE_TRANSITION: 500,
    ErrorCode.INTERNAL_ERROR: 500,
}


class MoaError(Exception):
    """携带 ErrorCode 的业务异常，由全局兜底统一映射为结构化响应。"""

    def __init__(self, code: ErrorCode | str, message: str = "") -> None:
        self.code = code if isinstance(code, ErrorCode) else ErrorCode(code)
        self.message = message or self.code.value
        super().__init__(self.message)


def http_status_for(code: ErrorCode | str) -> int:
    """错误码对应的 HTTP 状态码；未知码一律 500（不放大成 4xx）。"""
    try:
        key = code if isinstance(code, ErrorCode) else ErrorCode(code)
    except ValueError:
        return 500
    return HTTP_STATUS.get(key, 500)


__all__ = ["ErrorCode", "HTTP_STATUS", "MoaError", "http_status_for"]
