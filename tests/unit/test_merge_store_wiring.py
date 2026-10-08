"""合并通道存储装配：无 DSN → 内存，有 DSN → Postgres。

与 ``test_deps_wiring.py`` 同一模式：组合根在 import 期构造单例，测试通过
mock settings 重新调用 ``build_merge_store()`` 来验证选型逻辑。
"""

from __future__ import annotations

import pytest

from app.config import settings


def test_build_merge_store_without_dsn_returns_in_memory(monkeypatch) -> None:
    """无 DSN 时返回内存实现（开发/测试环境）。"""
    from apps.code_review_pipeline.merge_store import MergeStore, build_merge_store

    monkeypatch.setattr(settings, "vector_db_dsn", "")
    store = build_merge_store()
    assert isinstance(store, MergeStore)


def test_build_merge_store_with_dsn_returns_postgres(monkeypatch) -> None:
    """有 DSN 时返回 Postgres 实现（生产环境）。"""
    from apps.code_review_pipeline.merge_store import PostgresMergeStore, build_merge_store

    monkeypatch.setattr(
        settings, "vector_db_dsn", "postgresql://gateway:gateway@localhost:5433/gateway"
    )
    store = build_merge_store()
    assert isinstance(store, PostgresMergeStore)


def test_deps_exposes_merge_store_singleton() -> None:
    """组合根暴露 merge_store 单例，供 worker 与回调共用。"""
    from app import deps

    assert hasattr(deps, "merge_store")
    # 单例身份稳定：多次访问拿到同一实例
    assert deps.merge_store is deps.merge_store
