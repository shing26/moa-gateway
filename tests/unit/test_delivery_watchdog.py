"""delivery-watchdog-v1 验收测试：run heartbeat + stale-launch lock。

覆盖契约第 5 条：heartbeat 写入/更新/stop 删除、read_age_s 四态、失败
写入不外抛、锁的拒绝/抢锁/释放语义、flow 集成（heartbeat 在 run 期间
存在、run 结束后消失）。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.code_review_pipeline.change.pipeline import ChangeApproval
from apps.code_review_pipeline.delivery.flow import DeliveryFlow
from apps.code_review_pipeline.delivery.heartbeat import (
    DEFAULT_INTERVAL_S,
    Heartbeat,
    _default_interval_s,
    read_age_s,
)
from apps.code_review_pipeline.delivery.lockfile import (
    LockHandle,
    StaleLockReplaced,
    acquire,
    read_holder,
)
from apps.code_review_pipeline.delivery.models import DeliveryPolicy
from apps.code_review_pipeline.delivery.ports import (
    ChangePreparation,
    PreparationStatus,
)
from tests.unit.test_delivery_flow import (
    FakeApproval,
    FakeAudit,
    FakePreparer,
    FakePublisher,
    FakeRecorder,
    _attempt,
    _bundle,
    _request,
    _verification,
)

# ---------------------------------------------------------------------------
# heartbeat


def test_default_interval_reads_env_with_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DELIVERY_HEARTBEAT_S", raising=False)
    assert _default_interval_s() == DEFAULT_INTERVAL_S
    monkeypatch.setenv("DELIVERY_HEARTBEAT_S", "0.05")
    assert _default_interval_s() == 0.05
    monkeypatch.setenv("DELIVERY_HEARTBEAT_S", "not-a-number")
    assert _default_interval_s() == DEFAULT_INTERVAL_S
    monkeypatch.setenv("DELIVERY_HEARTBEAT_S", "-3")
    assert _default_interval_s() == DEFAULT_INTERVAL_S


@pytest.mark.asyncio
async def test_heartbeat_writes_atomically_immediately_and_updates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run-1.heartbeat"
    heartbeat = Heartbeat.start(path, interval_s=0.05)
    try:
        assert path.exists()
        first = path.read_text(encoding="utf-8").strip()
        # 原子写（temp + os.replace）成功后目录里没有 .tmp 残留。
        assert not list(tmp_path.glob("*.tmp"))
        stamp = datetime.fromisoformat(first)
        assert stamp.tzinfo is not None
        await asyncio.sleep(0.2)
        assert path.read_text(encoding="utf-8").strip() != first
    finally:
        heartbeat.stop()


@pytest.mark.asyncio
async def test_heartbeat_stop_deletes_file_and_cancels_task(tmp_path: Path) -> None:
    path = tmp_path / "run-1.heartbeat"
    heartbeat = Heartbeat.start(path, interval_s=0.05)
    assert path.exists()
    assert heartbeat.is_running()
    heartbeat.stop()
    assert not path.exists()
    assert not heartbeat.is_running()
    # stop 幂等：二次调用不抛。
    heartbeat.stop()


@pytest.mark.asyncio
async def test_heartbeat_update_within_interval(tmp_path: Path) -> None:
    path = tmp_path / "run-1.heartbeat"
    heartbeat = Heartbeat.start(path, interval_s=0.05)
    try:
        first = path.read_text(encoding="utf-8").strip()
        await asyncio.sleep(0.2)
        assert path.read_text(encoding="utf-8").strip() != first
        age = read_age_s(path)
        assert age is not None
        assert age < 1.0  # 刚刚刷新过，不是启动时的旧戳
    finally:
        heartbeat.stop()


@pytest.mark.asyncio
async def test_failing_heartbeat_write_never_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写失败（monkeypatch os.replace）只记日志：不外抛、任务不死。"""

    def broken_replace(src: object, dst: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", broken_replace)
    path = tmp_path / "run-1.heartbeat"
    # start() 的首写同样不能把异常带进 flow。
    heartbeat = Heartbeat.start(path, interval_s=0.03)
    await asyncio.sleep(0.1)
    assert heartbeat.is_running()  # 任务吞掉了 OSError，仍在运行
    assert not path.exists()
    heartbeat.stop()
    # 唯一允许的残留是 replace 失败留下的 .tmp 半成品——那正是"写失败
    # 不外抛"的形状；正式 heartbeat 文件必须不存在。
    leftovers = {item.name for item in tmp_path.iterdir()}
    assert leftovers <= {"run-1.heartbeat.tmp"}


def test_read_age_s_fresh_stale_missing_corrupt(tmp_path: Path) -> None:
    now = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
    fresh = tmp_path / "fresh.heartbeat"
    stale = tmp_path / "stale.heartbeat"
    fresh.write_text(
        (now - timedelta(seconds=5)).isoformat() + "\n", encoding="utf-8"
    )
    stale.write_text(
        (now - timedelta(seconds=300)).isoformat() + "\n", encoding="utf-8"
    )

    assert read_age_s(fresh, now=now) == pytest.approx(5.0)
    assert read_age_s(stale, now=now) == pytest.approx(300.0)
    assert read_age_s(tmp_path / "missing.heartbeat", now=now) is None
    corrupt = tmp_path / "corrupt.heartbeat"
    corrupt.write_text("<html>not a timestamp</html>", encoding="utf-8")
    assert read_age_s(corrupt, now=now) is None


# ---------------------------------------------------------------------------
# lockfile


def _holder_payload(pid: int, host: str = "other-host") -> dict[str, object]:
    return {
        "pid": pid,
        "started_at": datetime(2026, 11, 13, tzinfo=UTC).isoformat(),
        "host": host,
    }


def test_acquire_fresh_lock_records_our_identity(tmp_path: Path) -> None:
    path = tmp_path / ".locks" / "delivery-owner-name.lock"
    lock = acquire(path)
    assert isinstance(lock, LockHandle)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["host"]
    datetime.fromisoformat(str(payload["started_at"]))  # 合法 ISO 时间戳
    lock.release()
    assert not path.exists()


def test_acquire_refuses_when_holder_alive(tmp_path: Path) -> None:
    path = tmp_path / "delivery-owner-name.lock"
    path.write_text(json.dumps(_holder_payload(999)) + "\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    lock = acquire(path, alive=lambda pid: pid == 999)

    assert lock is None
    assert path.read_text(encoding="utf-8") == before
    holder = read_holder(path)
    assert holder is not None and holder.pid == 999


def test_acquire_steals_stale_lock_and_reports_previous(tmp_path: Path) -> None:
    path = tmp_path / "delivery-owner-name.lock"
    path.write_text(json.dumps(_holder_payload(999)) + "\n", encoding="utf-8")

    lock = acquire(path, alive=lambda pid: False)

    assert isinstance(lock, StaleLockReplaced)
    assert lock.previous.pid == 999
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert read_holder(path) is not None


def test_acquire_treats_corrupt_lock_as_stale(tmp_path: Path) -> None:
    path = tmp_path / "delivery-owner-name.lock"
    path.write_text("garbage{", encoding="utf-8")

    # alive 不该被问到：文件里没有可检查的 pid，锁视为 stale 抢走。
    lock = acquire(path, alive=lambda pid: True)

    assert isinstance(lock, StaleLockReplaced)
    assert lock.previous.pid is None
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_release_removes_only_our_own_lock(tmp_path: Path) -> None:
    path = tmp_path / "delivery-owner-name.lock"
    lock = acquire(path)
    assert isinstance(lock, LockHandle)

    # 别人抢走了锁（pid 与 host 都不同）→ release 是 no-op。
    path.write_text(json.dumps(_holder_payload(1234)) + "\n", encoding="utf-8")
    lock.release()
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == 1234

    # 换成我们自己的锁 → release 真的删文件。
    path.write_text(
        json.dumps(_holder_payload(os.getpid(), host=lock.info.host)) + "\n",
        encoding="utf-8",
    )
    lock.release()
    assert not path.exists()

    # 文件已经没了 → release 仍是安静 no-op。
    lock.release()


def test_read_holder_missing_or_corrupt(tmp_path: Path) -> None:
    assert read_holder(tmp_path / "absent.lock") is None
    bad = tmp_path / "bad.lock"
    bad.write_text("{not json", encoding="utf-8")
    assert read_holder(bad) is None


def test_cmd_delivery_run_refuses_live_lock_with_exit_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """契约第 4 条：活持有者在场时 CLI 打印 holder 并 exit 3，且在碰任何
    基础设施（merge_store/LLM/GitHub）之前就拒绝。"""
    from types import SimpleNamespace

    from app.cli import cmd_delivery_run
    from app.config import settings

    monkeypatch.setattr(settings, "change_agent_push_enabled", True)
    monkeypatch.setattr(settings, "delivery_topology", "single")
    monkeypatch.setattr(settings, "change_agent_model", "test-model")
    monkeypatch.setattr(settings, "change_agent_verify_command", "python -m pytest -q")
    monkeypatch.setattr(settings, "github_token", "token")
    monkeypatch.setattr(settings, "delivery_run_record_dir", str(tmp_path / "runs"))

    lock_path = tmp_path / "runs" / ".locks" / "delivery-owner-name.lock"
    lock_path.parent.mkdir(parents=True)
    # 用当前进程自己的 pid：真实 aliveness 检查必然判活，无需注入。
    lock_path.write_text(
        json.dumps(_holder_payload(os.getpid(), host="self")) + "\n",
        encoding="utf-8",
    )

    args = SimpleNamespace(repo="owner/name")
    assert cmd_delivery_run(args) == 3
    assert f"pid={os.getpid()}" in capsys.readouterr().err
    # 拒绝路径不动锁文件：持有者的锁原样保留。
    assert lock_path.exists()


def test_cmd_delivery_run_steals_stale_lock_and_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """契约第 4 条抢锁路径端到端：死持有者的锁被抢走 → 打警告 → run 体
    执行 → finally 里的 release() 不抛 AttributeError，并把锁文件删掉。"""
    from types import SimpleNamespace

    from app.cli import cmd_delivery_run
    from app.config import settings

    monkeypatch.setattr(settings, "change_agent_push_enabled", True)
    monkeypatch.setattr(settings, "delivery_topology", "single")
    monkeypatch.setattr(settings, "change_agent_model", "test-model")
    monkeypatch.setattr(settings, "change_agent_verify_command", "python -m pytest -q")
    monkeypatch.setattr(settings, "github_token", "token")
    monkeypatch.setattr(settings, "delivery_run_record_dir", str(tmp_path / "runs"))
    # 不碰真实基础设施：merge store guard 让 cmd 返回 2——关键是被抢锁的
    # 进程能带着锁走完 try/finally 而不崩。
    monkeypatch.setattr(
        "apps.code_review_pipeline.merge_store.build_merge_store",
        lambda: object(),
    )

    lock_path = tmp_path / "runs" / ".locks" / "delivery-owner-name.lock"
    lock_path.parent.mkdir(parents=True)
    # 真实 aliveness 检查下 pid 999999 必然已死 → 锁被视为 stale 抢走。
    lock_path.write_text(
        json.dumps(_holder_payload(999999)) + "\n", encoding="utf-8"
    )

    args = SimpleNamespace(repo="owner/name")
    assert cmd_delivery_run(args) == 2
    err = capsys.readouterr().err
    assert "replaced stale lock" in err
    assert "pid=999999" in err
    # finally 跑了 release()：锁文件（现在是我们自己的）被删。
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# flow integration (light)


class _HeartbeatProbeImplementer:
    """实现器探针：run 期间检查 heartbeat 文件是否在场。

    复用 test_delivery_flow 的 fake ports 惯例（FakeImplementer 委托），
    只加一个探针断言点。
    """

    def __init__(self, attempts: list, heartbeat_path: Path) -> None:
        self._attempts = list(attempts)
        self.heartbeat_path = heartbeat_path
        self.seen_during_run: bool | None = None

    async def run(self, **kwargs: object):
        from tests.unit.test_delivery_flow import FakeImplementer

        await asyncio.sleep(0)  # 让 heartbeat task 完成首写
        self.seen_during_run = self.heartbeat_path.exists()
        delegate = FakeImplementer(self._attempts)
        return await delegate.run(**kwargs)  # type: ignore[arg-type]


def _flow(
    tmp_path: Path,
    record_dir: Path,
    implementer: object,
    approval: object,
) -> DeliveryFlow:
    events: list[str] = []
    return DeliveryFlow(
        workspace=tmp_path / "workspace",
        policy=DeliveryPolicy(verify_command=("python", "-m", "pytest", "-q")),
        implementer=implementer,  # type: ignore[arg-type]
        preparer=FakePreparer(
            [
                ChangePreparation(
                    status=PreparationStatus.READY,
                    bundle=_bundle(),
                    verification=_verification(),
                )
            ]
        ),
        approval=approval,  # type: ignore[arg-type]
        publisher=FakePublisher(events),
        recorder=FakeRecorder(),
        audit=FakeAudit(events),
        new_run_id=lambda: "run-1",
        heartbeat_dir=record_dir,
    )


@pytest.mark.asyncio
async def test_flow_heartbeat_exists_during_run_and_gone_after(
    tmp_path: Path,
) -> None:
    """契约第 2/5 条：run 期间 heartbeat 在场，成功出口结束后消失。"""
    record_dir = tmp_path / "records"
    heartbeat_path = record_dir / "run-1.heartbeat"
    probe = _HeartbeatProbeImplementer([_attempt()], heartbeat_path)
    approval = FakeApproval(
        ChangeApproval(
            approver="alice",
            trace_id="trace-1",
            bundle_hash=_bundle().bundle_hash,
            run_id="run-1",
        )
    )
    flow = _flow(tmp_path, record_dir, probe, approval)

    run = await flow.run(_request())

    assert run.state.value == "waiting_ci"
    assert probe.seen_during_run is True
    assert not heartbeat_path.exists()


@pytest.mark.asyncio
async def test_flow_heartbeat_stops_on_rejected_path(tmp_path: Path) -> None:
    """非成功出口（rejected）同样停心跳、删文件。"""
    record_dir = tmp_path / "records"
    heartbeat_path = record_dir / "run-1.heartbeat"
    probe = _HeartbeatProbeImplementer([_attempt()], heartbeat_path)
    flow = _flow(tmp_path, record_dir, probe, FakeApproval(None))

    run = await flow.run(_request())

    assert run.state.value == "rejected"
    assert not heartbeat_path.exists()
