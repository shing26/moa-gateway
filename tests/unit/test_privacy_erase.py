from __future__ import annotations

import pytest

import app.routes.health as health_module


class _FailingVectorClient:
    async def delete_by_metadata(self, filter_metadata: dict) -> int:
        raise RuntimeError("vectordb down")


class _FakeRetriever:
    _client = _FailingVectorClient()


@pytest.mark.asyncio
async def test_privacy_erase_fails_closed_when_vectordb_delete_fails(monkeypatch):
    """B 类：删除失败必须传播，不能回 status=ok 让调用方误以为数据已清除。"""
    monkeypatch.setattr(health_module, "_retriever", _FakeRetriever())

    with pytest.raises(RuntimeError, match="vectordb down"):
        await health_module.privacy_erase("u1")
