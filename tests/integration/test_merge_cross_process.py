"""合并通道的跨进程可见性（ADR-021）。

合并通道由**两个进程**协作：网关（webhook 建记录、收回调）和 merge worker
（轮询 CI、发卡片）。两边各构造一份 merge_store，只有落在同一个库上才看得见
彼此的状态。

此前唯一的端到端用例（``test_merge_channel_e2e.py``）用的是进程内
``MergeStore()``，证明的是单进程逻辑——而"网关写了、worker 看不看得见"这条
恰恰没被覆盖，也正是无共享库时静默失效的那条。本文件用**两个独立 store 实例**
对着真库钉住这条契约。

需要：
1. Postgres 容器在跑（docker compose -f docker-compose.dev.yml up -d postgres）
2. ``CODE_REVIEW_DATABASE_URL`` 已配置，否则整个模块 skip
"""

from __future__ import annotations

import os
import uuid

import pytest

from apps.code_review_pipeline.merge_state import MergeAction, MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, PostgresMergeStore

pytestmark = pytest.mark.skipif(
    not os.getenv("CODE_REVIEW_DATABASE_URL"),
    reason="CODE_REVIEW_DATABASE_URL not set",
)

_REPO_PREFIX = "test/xproc-"


def _dsn() -> str:
    dsn = os.getenv("CODE_REVIEW_DATABASE_URL")
    if not dsn:
        pytest.skip("CODE_REVIEW_DATABASE_URL not set")
    return dsn


def _identity() -> tuple[str, int, str]:
    suffix = uuid.uuid4().hex[:8]
    return (f"{_REPO_PREFIX}{suffix}", 999, suffix)


@pytest.fixture
def stores() -> tuple[PostgresMergeStore, PostgresMergeStore]:
    """两个独立实例，模拟网关进程与 merge worker 进程各建一份。"""
    gateway = PostgresMergeStore(dsn=_dsn())
    worker = PostgresMergeStore(dsn=_dsn())
    gateway._connect()
    with gateway._conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pr_merge_gate WHERE repo LIKE %s", (f"{_REPO_PREFIX}%",)
        )
        gateway._conn.commit()
    return gateway, worker


def test_worker_sees_record_created_by_gateway(
    stores: tuple[PostgresMergeStore, PostgresMergeStore],
) -> None:
    """网关在 webhook 里建记录，worker 轮询时必须能拿到它。"""
    gateway, worker = stores
    identity = _identity()
    gateway.save(
        MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
    )

    open_in_worker = [r for r in worker.list_open() if r.repo == identity[0]]
    assert [r.head_sha for r in open_in_worker] == [identity[2]]


def test_gateway_sees_state_advanced_by_worker(
    stores: tuple[PostgresMergeStore, PostgresMergeStore],
) -> None:
    """worker 推进状态后，网关收审批回调时读到的必须是新状态。

    这正是无共享库时会静默断掉的一跳：两个进程各持一份内存，谁也看不见谁。
    """
    gateway, worker = stores
    identity = _identity()
    gateway.save(
        MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
    )

    worker.transition(identity, MergeAction.CI_GREEN, at="t1")

    got = gateway.get(identity)
    assert got is not None
    assert got.state == MergeState.AWAITING_APPROVAL.value
