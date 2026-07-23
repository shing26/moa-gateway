from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.channels.base import ChannelAdapter, ChannelMessage

logger = logging.getLogger("moa.channels.feishu")


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    base_url: str = "https://open.feishu.cn/open-api"


class FeishuChannelAdapter(ChannelAdapter):
    def __init__(self, config: FeishuConfig, *, timeout: float = 10.0) -> None:
        self.config = config
        self.timeout = timeout

    async def send(self, message: ChannelMessage) -> bool:
        if message.channel != "feishu":
            return False
        try:
            token = await self._tenant_access_token()
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

    async def _tenant_access_token(self) -> str:
        url = f"{self.config.base_url}/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.config.app_id, "app_secret": self.config.app_secret}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Feishu auth failed: {data}")
            return str(data["tenant_access_token"])
