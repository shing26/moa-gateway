from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.channels.feishu_auth import FeishuTokenProvider

logger = logging.getLogger("moa.channels.feishu_cards")


@dataclass
class ApprovalCard:
    session_id: str
    trace_id: str
    agent_name: str
    intent: str
    agent_output: str
    channel: str
    target: str

    def to_card_payload(self) -> dict[str, Any]:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "MoA Engine - 人工审批请求"},
                "template": "orange",
            },
            "elements": [
                {"tag": "markdown", "content": f"**Agent**: {self.agent_name}"},
                {"tag": "markdown", "content": f"**Intent**: {self.intent}"},
                {"tag": "markdown", "content": f"**Trace**: {self.trace_id}"},
                {"tag": "hr"},
                {"tag": "markdown", "content": f"**Agent Output**:\n`\n{self.agent_output[:2000]}\n`"},
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
            ],
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
