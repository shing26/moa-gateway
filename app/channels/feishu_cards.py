from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.channels.feishu import FeishuConfig

logger = logging.getLogger("moa.channels.feishu_cards")


@dataclass
class ApprovalCard:
    """A Feishu interactive card for HITL approval."""

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
                "title": {"tag": "plain_text", "content": "MoA Engine — 人工审批请求"},
                "template": "orange",
            },
            "elements": [
                {"tag": "markdown", "content": f"**Agent**: `{self.agent_name}`"},
                {"tag": "markdown", "content": f"**Intent**: `{self.intent}`"},
                {"tag": "markdown", "content": f"**Trace**: `{self.trace_id}`"},
                {"tag": "hr"},
                {"tag": "markdown", "content": f"**Agent Output**:\n```\n{self.agent_output[:2000]}\n```"},
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✅ 批准"},
                            "value": {
                                "action": "approve",
                                "session_id": self.session_id,
                                "trace_id": self.trace_id,
                            },
                            "type": "primary",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                            "value": {
                                "action": "reject",
                                "session_id": self.session_id,
                                "trace_id": self.trace_id,
                            },
                            "type": "danger",
                        },
                    ],
                },
            ],
        }

    def to_message_payload(self, tenant_access_token: str) -> dict[str, Any]:
        content = json.dumps(self.to_card_payload(), ensure_ascii=False)
        return {
            "receive_id": self.target,
            "msg_type": "interactive",
            "content": content,
        }


class FeishuCardSender:
    """Sends interactive approval cards via Feishu API."""

    def __init__(self, config: FeishuConfig) -> None:
        self.config = config
        self._token: str | None = None

    async def _ensure_token(self) -> str:
        if self._token:
            return self._token
        url = f"{self.config.base_url}/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.config.app_id, "app_secret": self.config.app_secret}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Feishu auth failed: {data}")
            self._token = str(data["tenant_access_token"])
            return self._token

    async def send_card(self, card: ApprovalCard) -> bool:
        try:
            token = await self._ensure_token()
            payload = card.to_message_payload(token)
            url = f"{self.config.base_url}/im/v1/messages"
            params = {"receive_id_type": "chat_id"}
            async with httpx.AsyncClient(timeout=10.0) as client:
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

    def invalidate_token(self) -> None:
        self._token = None


def parse_card_callback(body: dict[str, Any]) -> tuple[str, str, str] | None:
    """Extract (session_id, trace_id, action) from a Feishu card action callback.

    Returns None if the callback doesn't contain card action data.
    """
    try:
        action_data = body.get("action", {})
        value = action_data.get("value", {})
        if not value:
            # Some Feishu callback formats nest differently
            value = body.get("value", {})
        session_id = value.get("session_id", "")
        trace_id = value.get("trace_id", "")
        action = value.get("action", "")
        if not session_id or not action:
            return None
        return session_id, trace_id, action
    except Exception:
        return None
