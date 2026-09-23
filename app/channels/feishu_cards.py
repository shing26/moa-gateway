from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.channels.feishu_auth import FeishuTokenProvider

logger = logging.getLogger("moa.channels.feishu_cards")

# 卡片外观按审批来源切换。失败升级卡片里没有"待批准的输出"，只有失败详情；
# 沿用审批卡片的标题与标签会让人误以为有内容要批，所以这里显式区分。
_HITL_TITLES = {
    "failure_escalation": "Agent Gateway - 失败升级待处理",
    "notification": "Agent Gateway - 通知",
}
_HITL_TEMPLATES = {"failure_escalation": "red", "notification": "blue"}
_HITL_OUTPUT_LABELS = {"failure_escalation": "失败详情", "notification": "详情"}
# 只有真的接了审批闭环的卡片才该有按钮。"notification" 用于**没有审批落地**的链路
# （如 PR 审查报告）：那条路径从不 store_hitl，渲染审批按钮等于承诺一个点了必然
# 失效的动作（2026-09-23 修正）。
_NO_ACTION_KINDS = frozenset({"notification"})


@dataclass
class ApprovalCard:
    session_id: str
    trace_id: str
    agent_name: str
    intent: str
    agent_output: str
    channel: str
    target: str
    # 审批来源：guard 策略判定（"review"）／评估器判定（"eval_review"）／
    # 自动处理失败后的升级（"failure_escalation"）。默认值让既有构造点不变。
    hitl_kind: str = "review"

    def to_card_payload(self) -> dict[str, Any]:
        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": f"**Agent**: {self.agent_name}"},
            {"tag": "markdown", "content": f"**Intent**: {self.intent}"},
            {"tag": "markdown", "content": f"**Trace**: {self.trace_id}"},
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": (
                    f"**{_HITL_OUTPUT_LABELS.get(self.hitl_kind, 'Agent Output')}**:\n"
                    f"`\n{self.agent_output[:2000]}\n`"
                ),
            },
        ]
        if self.hitl_kind not in _NO_ACTION_KINDS:
            elements += [
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "批准"},
                            "value": {"action": "approve", "session_id": self.session_id, "trace_id": self.trace_id},
                            "type": "primary",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "拒绝"},
                            "value": {"action": "reject", "session_id": self.session_id, "trace_id": self.trace_id},
                            "type": "danger",
                        },
                    ],
                },
            ]
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": _HITL_TITLES.get(
                        self.hitl_kind, "Agent Gateway - 人工审批请求"
                    ),
                },
                "template": _HITL_TEMPLATES.get(self.hitl_kind, "orange"),
            },
            "elements": elements,
        }

    def to_message_payload(self) -> dict[str, Any]:
        content = json.dumps(self.to_card_payload(), ensure_ascii=False)
        return {"receive_id": self.target, "msg_type": "interactive", "content": content}


class FeishuCardSender:
    def __init__(self, auth: FeishuTokenProvider, *, timeout: float = 10.0) -> None:
        self._auth = auth
        self.timeout = timeout

    async def send_card(self, card: ApprovalCard) -> bool:
        try:
            token = await self._auth.get_token()
            payload = card.to_message_payload()
            url = f"{self._auth.config.base_url}/im/v1/messages"
            params = {"receive_id_type": "chat_id"}
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    params=params,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    logger.error("feishu card send failed: %s", data)
                    return False
                logger.info("feishu approval card sent session=%s", card.session_id)
                return True
        except httpx.HTTPStatusError as exc:
            # 飞书对畸形请求体/参数返回 HTTP 4xx，错误原因在响应体里，必须记下来才能定位
            body = exc.response.text[:500] if exc.response is not None else ""
            logger.error("feishu card send http error: %s body=%s", exc, body)
            return False
        except Exception as exc:
            logger.exception("feishu card send error: %s", exc)
            return False


def parse_card_callback(body: dict[str, Any]) -> tuple[str, str, str] | None:
    try:
        value = body.get("action", {}).get("value", {}) or body.get("value", {})
        session_id = value.get("session_id", "")
        trace_id = value.get("trace_id", "")
        action = value.get("action", "")
        if not session_id or not action:
            return None
        return session_id, trace_id, action
    except Exception:
        return None
