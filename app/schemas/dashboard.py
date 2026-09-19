"""dashboard JSON API 的请求体模型（M3 拆分：原 dashboard.py 内联定义）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class UploadDoc(BaseModel):
    title: str
    content: str


class ModeUpdate(BaseModel):
    mode: str


class SearchQuery(BaseModel):
    query: str
    top_k: int = 5


class OpsConfigUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class OpsTestRequest(BaseModel):
    message: str = "ping"
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class FlagUpdate(BaseModel):
    value: Any


__all__ = [
    "FlagUpdate",
    "ModeUpdate",
    "OpsConfigUpdate",
    "OpsTestRequest",
    "SearchQuery",
    "UploadDoc",
]
