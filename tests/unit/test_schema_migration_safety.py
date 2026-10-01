"""建表迁移不得无限期阻塞，且必须可关闭（2026-10-01）。

真库实测出来的缺陷链（`uv run pytest tests/integration` 直接挂死 >60s）：

1. ``_ensure_schema`` 把整个 schema.sql 一次性 execute，其中包含
   ``ALTER TABLE ... ALTER COLUMN SET DEFAULT`` 与非 CONCURRENTLY 的
   ``CREATE UNIQUE INDEX`` —— 两者都需要 **ACCESS EXCLUSIVE** 锁。
2. 任何一条**只读**连接（哪怕只是一个没提交的 SELECT，它握 ACCESS SHARE）
   都会让 ACCESS EXCLUSIVE 申请排队等待。
3. PG 的锁等待**默认没有超时**，于是不是"失败"而是"挂死"。

对 D3 来说这不是理论问题：worker 与 gateway 是两个独立进程，两者都在启动时
跑迁移。只要 worker 手里有任何一条在途查询，gateway 就会在启动时永久挂住，
且日志上一片安静——与 D1 修掉的 connect_timeout 缺失是同一族缺陷
（"配了却连不上"表现为进程卡住，而不是快速失败）。

两条修法：给迁移连接加 lock_timeout（把无界等待换成明确异常）；给迁移加开关
（worker 不该在启动时改 schema，DDL 归 gateway 或显式 CLI）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SCHEMA_SQL = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "code_review_pipeline"
    / "storage"
    / "schema.sql"
).read_text(encoding="utf-8")


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []


def _fake_psycopg(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()

    class _Cur:
        def __enter__(self) -> "_Cur":
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

        def cursor(self) -> _Cur:
            return _Cur()

        def commit(self) -> None:
            return None

    class _Fake:
        @staticmethod
        def connect(dsn: str, **kwargs: Any) -> _Conn:
            rec.calls.append({"dsn": dsn, **kwargs})
            return _Conn()

    monkeypatch.setitem(sys.modules, "psycopg", _Fake)
    monkeypatch.setattr(
        "apps.code_review_pipeline.storage.review_store._missing_deps", list
    )
    return rec


def test_ensure_schema_sets_lock_timeout_via_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """迁移连接必须带 lock_timeout，且必须走 libpq 认的 ``options``。

    踩过的坑：``lock_timeout`` 直传成 ``psycopg.connect(..., lock_timeout=15000)``
    在真库上报 ``invalid connection option "lock_timeout"``——它是 PG 的**服务端
    运行参数**，不是 libpq 连接选项。而假 psycopg 对任何 kwarg 都点头，所以
    "断言传了这个 kwarg" 的单测是绿的、行为是坏的。这类缺陷只有真库能证伪，
    见 tests/integration 里那条真会超时的用例。
    """
    from apps.code_review_pipeline.storage import review_store as rs

    rec = _fake_psycopg(monkeypatch)
    rs._ensure_schema("postgresql://u:p@h:5432/db")
    kwargs = rec.calls[0]
    assert "lock_timeout" not in kwargs, (
        "lock_timeout 不能作为连接参数直传，libpq 不认，真库会直接失败"
    )
    options = kwargs.get("options", "")
    assert "lock_timeout=" in options, (
        f"lock_timeout 必须经 options 传给服务端，实际 options={options!r}"
    )
    millis = int(options.split("lock_timeout=")[1].split()[0])
    # 单位是毫秒。写错成秒等于给了 15000 秒——比不设还糟。
    assert 0 < millis <= 60_000, f"lock_timeout={millis}ms 超出一分钟"


def test_schema_contains_access_exclusive_operations() -> None:
    """锁危险的来源显式记录在案：ALTER 与非 CONCURRENTLY 建索引。

    给未来的自己留一张地图：哪天有人问"为什么迁移要加 lock_timeout"，答案在这条
    断言的注释里。顺带防止有人把索引改成 CONCURRENTLY 却没意识到 DDL 语义已变
    （CONCURRENTLY 不能与其它语句放在同一个 execute 里）。
    """
    upper = SCHEMA_SQL.upper()
    assert "ALTER TABLE CODE_REVIEW_PRS" in upper
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in upper
    assert "CONCURRENTLY" not in upper


def test_auto_migrate_disabled_skips_ddl_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关关着时连库都不该连——这才是"worker 不碰 schema"的含义。

    只加 flag 但仍然建连接没有意义：连接本身就已经把 worker 绑在 PG 的可用性上。
    """
    from app.config import settings
    from apps.code_review_pipeline.storage import review_store as rs

    rec = _fake_psycopg(monkeypatch)
    monkeypatch.setattr(rs, "_build_dsn", lambda: "postgresql://u:p@h:5432/db")
    monkeypatch.setattr(settings, "code_review_auto_migrate", False)

    store = rs.build_review_store()
    assert isinstance(store, rs.PostgresReviewStore)
    assert rec.calls == [], "开关关着却还是连库跑 DDL 了"
