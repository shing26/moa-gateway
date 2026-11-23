"""合并通道启动自检（ADR-021）。

合并通道的失败模式是**静默不生效**：worker 没起、卡片没目标、HITL 存储退化成
进程内存，都不会让任何东西报错，只是通道永远不动。启动时把前置条件逐条查一遍
并明确说出来，比事后在飞书里等一张永远不来的卡片便宜得多。

配置层面的"必须有共享库"由 app/config.py 在启动时直接 raise（不是这里的事）；
本模块负责那些**只有跑起来才知道**的条件。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from apps.code_review_pipeline.merge_preflight import (
    MERGE_WORKER_HEARTBEAT_KEY,
    hitl_store_is_shared,
    merge_channel_problems,
    merge_channel_readiness,
    read_worker_heartbeat_age_s,
)


class _FakeStorage:
    """只回答两件事：被读过吗、现在是不是退化成内存了。"""

    def __init__(self, *, using_memory: bool) -> None:
        self.reads: list[str] = []
        self._using_memory = using_memory

    def key(self, hitl_id: str) -> str:
        return f"moa:hitl:{hitl_id}"

    def get(self, key: str) -> str | None:
        self.reads.append(key)
        return None

    @property
    def using_memory(self) -> bool:
        return self._using_memory


class _FakeSessionStore:
    def __init__(self, storage: object | None) -> None:
        self._storage = storage


def test_hitl_shared_when_storage_is_not_in_memory() -> None:
    storage = _FakeStorage(using_memory=False)
    assert hitl_store_is_shared(_FakeSessionStore(storage)) is True
    # 必须真的读一次：连接/回退的判定是在第一次操作时发生的，不探测就永远是初值。
    assert storage.reads == ["moa:hitl:__preflight__"]


def test_hitl_not_shared_when_fallback_to_memory() -> None:
    assert hitl_store_is_shared(_FakeSessionStore(_FakeStorage(using_memory=True))) is False


def test_hitl_not_shared_without_storage() -> None:
    """没有注入 storage 的 SessionStore 就是进程内存实现。"""
    assert hitl_store_is_shared(_FakeSessionStore(None)) is False


class _ExplodingStorage:
    """``REDIS_ENABLE_FALLBACK=0`` 且 Redis 不可达时的真实行为：探测直接抛。"""

    def key(self, hitl_id: str) -> str:
        return f"moa:hitl:{hitl_id}"

    def get(self, key: str) -> str | None:
        raise ConnectionError("redis unavailable and fallback disabled")


def test_probe_failure_is_reported_not_raised() -> None:
    """自检绝不能让网关起不来。

    探测抛出的异常若冒到 lifespan（app/main.py），整个网关——包括和合并无关的
    聊天主路径——都起不来。一个附加能力不该有这个权力（同 app/deps.py 里
    「协作只是新增能力，不该有这个权力」那条）。探测失败就是"没共享"，报出来即可。
    """
    assert hitl_store_is_shared(_FakeSessionStore(_ExplodingStorage())) is False


def test_all_prerequisites_met_has_no_problems() -> None:
    assert (
        merge_channel_problems(
            channel="oc_abc",
            feishu_configured=True,
            hitl_shared=True,
            worker_heartbeat_age_s=1.0,
        )
        == []
    )


def test_missing_channel_is_reported() -> None:
    problems = merge_channel_problems(
        channel="",
        feishu_configured=True,
        hitl_shared=True,
        worker_heartbeat_age_s=1.0,
    )
    assert len(problems) == 1
    assert "CODE_REVIEW_MERGE_CHANNEL" in problems[0]


def test_missing_feishu_credentials_are_reported() -> None:
    problems = merge_channel_problems(
        channel="oc_abc",
        feishu_configured=False,
        hitl_shared=True,
        worker_heartbeat_age_s=1.0,
    )
    assert len(problems) == 1
    assert "FEISHU_APP_ID" in problems[0]


def test_unshared_hitl_store_is_reported() -> None:
    """Redis 不可达时 HITL 存储退化成进程内存：worker 登记的待批记录网关认领不到，
    人点了按钮只会得到「已失效」。这条不报出来，故障现场是「按钮没反应」。"""
    problems = merge_channel_problems(
        channel="oc_abc",
        feishu_configured=True,
        hitl_shared=False,
        worker_heartbeat_age_s=1.0,
    )
    assert len(problems) == 1
    assert "Redis" in problems[0]


def test_multiple_problems_are_all_reported() -> None:
    """一次列全，而不是让人改一条重启一次再发现下一条。"""
    problems = merge_channel_problems(
        channel="",
        feishu_configured=False,
        hitl_shared=False,
        worker_heartbeat_age_s=1.0,
    )
    assert len(problems) == 3


def test_readiness_is_silent_when_channel_disabled() -> None:
    """通道关着的时候不该报任何问题——默认配置下启动日志必须是干净的。"""
    assert (
        merge_channel_readiness(
            enabled=False,
            channel="",
            feishu_configured=False,
            session_store=_FakeSessionStore(None),
            worker_heartbeat_age_s=None,
        )
        == []
    )


def test_readiness_reports_problems_when_enabled() -> None:
    problems = merge_channel_readiness(
        enabled=True,
        channel="",
        feishu_configured=False,
        session_store=_FakeSessionStore(_FakeStorage(using_memory=True)),
        worker_heartbeat_age_s=1.0,
    )
    assert len(problems) == 3


def test_readiness_clean_when_everything_present() -> None:
    assert (
        merge_channel_readiness(
            enabled=True,
            channel="oc_abc",
            feishu_configured=True,
            session_store=_FakeSessionStore(_FakeStorage(using_memory=False)),
            worker_heartbeat_age_s=1.0,
        )
        == []
    )


def test_missing_worker_heartbeat_is_reported_with_start_command() -> None:
    """T4：通道开着但 worker 没起来——这是静默失败，必须点名启动命令。"""
    problems = merge_channel_problems(
        channel="oc_abc",
        feishu_configured=True,
        hitl_shared=True,
        worker_heartbeat_age_s=None,
    )

    assert len(problems) == 1
    assert "python -m app.merge_worker" in problems[0]


def test_fresh_worker_heartbeat_is_not_reported() -> None:
    """T4：心跳新鲜时不多报一条。"""
    problems = merge_channel_problems(
        channel="oc_abc",
        feishu_configured=True,
        hitl_shared=True,
        worker_heartbeat_age_s=1.0,
    )

    assert not any("app.merge_worker" in problem for problem in problems)
    assert problems == []


def test_stale_worker_heartbeat_is_reported() -> None:
    """T4：心跳过期等价于 worker 卡住/停止，同样要报。"""
    problems = merge_channel_problems(
        channel="oc_abc",
        feishu_configured=True,
        hitl_shared=True,
        worker_heartbeat_age_s=10_000.0,
    )

    assert len(problems) == 1
    assert "python -m app.merge_worker" in problems[0]


def test_readiness_is_silent_when_channel_disabled_even_without_heartbeat() -> None:
    """通道关闭时不报心跳问题（默认部署日志保持干净）。"""
    assert (
        merge_channel_readiness(
            enabled=False,
            channel="",
            feishu_configured=False,
            session_store=_FakeSessionStore(None),
            worker_heartbeat_age_s=None,
        )
        == []
    )


class _HeartbeatClient:
    def __init__(self, value: object) -> None:
        self.value = value
        self.keys: list[str] = []

    async def get(self, key: str) -> object:
        self.keys.append(key)
        return self.value


@pytest.mark.asyncio
async def test_read_heartbeat_age_from_json() -> None:
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    client = _HeartbeatClient(
        json.dumps(
            {
                "pid": 123,
                "interval_s": 60.0,
                "at": (now - timedelta(seconds=12)).isoformat(),
            }
        )
    )

    age = await read_worker_heartbeat_age_s(client=client, now=now)

    assert age == 12.0
    assert client.keys == [MERGE_WORKER_HEARTBEAT_KEY]


@pytest.mark.asyncio
async def test_read_heartbeat_age_accepts_z_suffix() -> None:
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    client = _HeartbeatClient(
        json.dumps({"pid": 1, "interval_s": 60.0, "at": "2026-11-24T11:59:00Z"})
    )

    assert await read_worker_heartbeat_age_s(client=client, now=now) == 60.0


@pytest.mark.asyncio
async def test_missing_heartbeat_returns_none() -> None:
    assert (
        await read_worker_heartbeat_age_s(client=_HeartbeatClient(None), now=datetime.now(UTC))
        is None
    )


@pytest.mark.asyncio
async def test_malformed_heartbeat_returns_none() -> None:
    assert (
        await read_worker_heartbeat_age_s(
            client=_HeartbeatClient("not json"), now=datetime.now(UTC)
        )
        is None
    )


class _ExplodingHeartbeatClient:
    async def get(self, key: str) -> object:
        raise ConnectionError("redis unavailable")


@pytest.mark.asyncio
async def test_heartbeat_read_failure_returns_none_and_never_raises() -> None:
    assert (
        await read_worker_heartbeat_age_s(
            client=_ExplodingHeartbeatClient(), now=datetime.now(UTC)
        )
        is None
    )
