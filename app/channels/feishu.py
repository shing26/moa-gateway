from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.channels.base import ChannelAdapter, ChannelMessage
from app.channels.feishu_auth import FeishuTokenProvider

logger = logging.getLogger("moa.channels.feishu")


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    base_url: str = "https://open.feishu.cn/open-api"


class FeishuChannelAdapter(ChannelAdapter):
    def __init__(self, config: FeishuConfig, *, auth: FeishuTokenProvider | None = None, timeout: float = 10.0) -> None:
        self.config = config
        self._auth = auth or FeishuTokenProvider(config)
        self.timeout = timeout

    async def send(self, message: ChannelMessage) -> bool:
        if message.channel != "feishu":
            return False
        try:
            token = await self._auth.get_token()
            url = f"{self.config.base_url}/im/v1/messages"
            params = {"receive_id_type": "chat_id"}
            payload = {
                "receive_id": message.target,
                "msg_type": "text",
                "content": '{"text":"' + message.text.replace('"', '\\"') + '"}',
            }
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
                    logger.error("feishu send failed: %s", data)
                    return False
                return True
        except Exception as exc:
            logger.exception("feishu send error: %s", exc)
            return False
