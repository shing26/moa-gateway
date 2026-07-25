from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("moa.channels.feishu_auth")


@dataclass
class FeishuAuthConfig:
    app_id: str
    app_secret: str
    base_url: str = "https://open.feishu.cn/open-api"
    token_ttl: float = 5400  # Feishu token 有效 2h，提前 10min 刷新


class FeishuTokenProvider:
    """Shared Feishu tenant access token provider with caching.

    Both FeishuChannelAdapter and FeishuCardSender use this
    instead of duplicating the token acquisition logic.
    """

    def __init__(self, config: FeishuAuthConfig) -> None:
        self.config = config
        self._token: str = ""
        self._expires: float = 0.0

    async def get_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._expires:
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
        self._expires = now + self.config.token_ttl
        logger.info("feishu token refreshed, expires in %.0fs", self.config.token_ttl)
        return self._token

    def invalidate(self) -> None:
        self._token = ""
        self._expires = 0.0
