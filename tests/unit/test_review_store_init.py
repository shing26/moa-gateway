"""Review store 初始化期不得做无界网络等待（2026-10-01）。

缺陷背景（实测复现）：库不可达时，``uv run python -c "import app.main"``
**挂死 >2.5 分钟**。两个成因：

1. ``_ensure_schema`` / ``PostgresReviewStore._connect`` 直接 ``psycopg.connect(dsn)``，
   不给 ``connect_timeout``。库不可达时走 OS 级 TCP 重传，Windows 上是分钟级，
   于是"配了库但库没起"表现为**进程卡住**而非快速失败 —— 最坏的一类故障：
   部署时表现为服务起不来，且没有任何日志说明卡在哪。
2. ``github_review_route`` 在**模块级**执行 ``build_review_store()``。import 阶段
   就发起建表连接，于是"只想 import 看一眼"的诊断动作也变成一次真实网络等待。

对照 ``app/vectordb/pgvector_client.py``：那边既有 ``open_timeout_s``，也有
"startup must not explode" 的降级（只有 ``_strict`` 才 raise）。review_store 是
同源存储，却是同一条链上唯一没有超时约束的一环。
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest

from apps.code_review_pipeline.storage import review_store as rs


class _ConnectRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []


def _install_fake_psycopg(monkeypatch: pytest.MonkeyPatch) -> _ConnectRecorder:
    """替换 psycopg，记录 ``connect`` 实际收到的 kwargs。"""
    recorder = _ConnectRecorder()

    class _Cursor:
        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, *a: object, **k: object) -> None:
            return None

    class _Conn:
        def __enter__(self) -> "_Conn":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor()

        def commit(self) -> None:
            return None

    class _FakePsycopg:
        @staticmethod
        def connect(dsn: str, **kwargs: Any) -> _Conn:
            recorder.calls.append({"dsn": dsn, **kwargs})
            return _Conn()

    monkeypatch.setitem(sys.modules, "psycopg", _FakePsycopg)
    monkeypatch.setattr(rs, "_missing_deps", list)
    return recorder


# ── 缺陷 1：两处 connect 都必须带 connect_timeout ────────────────────────


def test_ensure_schema_passes_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _install_fake_psycopg(monkeypatch)
    rs._ensure_schema("postgresql://u:p@127.0.0.1:5432/db")

    assert len(recorder.calls) == 1
    timeout = recorder.calls[0].get("connect_timeout")
    assert timeout is not None, "建表连接没有 connect_timeout：库不可达时会无界等待"
    assert 0 < float(timeout) <= 30, "超时值要么缺失，要么大到没有约束力"


def test_postgres_store_connect_passes_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _install_fake_psycopg(monkeypatch)
    store = rs.PostgresReviewStore(dsn="postgresql://u:p@127.0.0.1:5432/db")
    store._connect()

    assert len(recorder.calls) == 1
    assert recorder.calls[0].get("connect_timeout") is not None


def test_postgres_store_surfaces_unreachable_db_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库不可达时抛的是**限时**异常，而不是无限期挂起。"""
    import psycopg

    def _boom(dsn: str, **kwargs: Any):
        assert kwargs.get("connect_timeout") is not None, "缺少超时就把无界等待放行了"
        raise psycopg.OperationalError("connection timed out")

    monkeypatch.setattr(psycopg, "connect", _boom)
    store = rs.PostgresReviewStore(dsn="postgresql://u:p@127.0.0.1:5432/db")
    with pytest.raises(psycopg.OperationalError):
        store._connect()


# ── 缺陷 2：import 阶段不得做网络 I/O ───────────────────────────────────


def test_importing_review_route_does_not_touch_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``import github_review_route`` 不得触发任何建表连接。

    回归：模块级 ``_review_store = build_review_store()`` 让"只想 import 看看"
    的诊断动作（``import app.main``、pytest 收集、CLI 启动）全部变成一次真实网络等待。
    """
    calls: list[str] = []
    monkeypatch.setattr(rs, "_build_dsn", lambda: "postgresql://u:p@127.0.0.1:5432/db")
    monkeypatch.setattr(rs, "_ensure_schema", lambda dsn: calls.append(dsn))

    import apps.code_review_pipeline.routing.github_review_route as route

    importlib.reload(route)
    assert calls == [], "import 阶段就建立了数据库连接"


def test_review_route_store_is_built_on_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """延迟不等于丢失：真正写记录时仍然要建 store，且只建一次。"""
    import apps.code_review_pipeline.routing.github_review_route as route

    built: list[str] = []
    sentinel = object()

    def _build() -> object:
        built.append("once")
        return sentinel

    monkeypatch.setattr(route, "build_review_store", _build)
    monkeypatch.setattr(route, "_review_store", None, raising=False)

    assert route._get_review_store() is sentinel
    assert route._get_review_store() is sentinel
    assert built == ["once"], "每次请求都重建 store = 每次请求都建连接"
