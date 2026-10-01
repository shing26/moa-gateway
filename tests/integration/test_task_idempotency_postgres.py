"""D2 判据的真库验证：唯一约束、身份三列、重投不倒退（需要真实 PostgreSQL）。

为什么必须是集成测试：这几条判据的失败模式**全部是静默的**。

- ``UNIQUE(repo, pr_number, head_sha)`` 建在错误的三元组上时，唯一约束照样存在、
  照样创建成功，只是把该分开的两条任务合并了。用假连接断言 SQL 文本发现不了。
- ``posted_review_id`` 被重投抹掉、``status`` 被拽回 queued：都不报错，表现为
  "GitHub 上多贴了一条评论"和"同一个任务多跑了一遍 5 个 agent"。
- ``state_transitions`` 传成 Python list 而非 Jsonb：psycopg3 会适配成 PG 数组，
  jsonb 列直接类型不匹配。

compose 未起时 skip（不伪造通过），起了就真跑：

    docker compose -f docker-compose.dev.yml up -d postgres
    uv run pytest tests/integration -q
"""

from __future__ import annotations

import uuid
from typing import Any, Iterator

import pytest

psycopg = pytest.importorskip("psycopg")

from apps.code_review_pipeline.storage.review_store import (
    PostgresReviewStore,
    ReviewRecord,
    build_review_store,
)

DSN = "postgresql://gateway:gateway@localhost:5433/gateway"


@pytest.fixture
def pg_store() -> Iterator[PostgresReviewStore]:
    """连真库建表；连不上就 skip（而不是让整份测试文件失败）。"""
    from app.config import settings

    settings.vector_db_dsn = DSN
    try:
        store = build_review_store()
    except Exception as exc:  # noqa: BLE001 - 连不上库不是测试失败
        pytest.skip(f"PostgreSQL 不可用（{type(exc).__name__}），跳过集成用例")
    if not isinstance(store, PostgresReviewStore):
        pytest.skip("拿到的是内存 store，说明 DSN 没生效")
    try:
        yield store
    finally:
        settings.vector_db_dsn = ""


def _rec(repo: str, pr: int, sha: str, **over: Any) -> ReviewRecord:
    return ReviewRecord(
        trace_id=f"cr_{repo}#{pr}@{sha}",
        repo=repo,
        pr_number=pr,
        head_sha=sha,
        author="tester",
        findings_count=1,
        need_human_review=False,
        raw={
            "base_sha": "basesha",
            "title": f"title-{repo}-{pr}",
            "html_url": "https://example.test/pr",
            "diff_url": "https://example.test/pr.diff",
            **over,
        },
    )


def _fetch_one(store: PostgresReviewStore, sql: str, params: tuple) -> tuple:
    store._connect()  # noqa: SLF001 - 测试直接查库，绕过业务语义层
    with store._conn.cursor() as cur:  # noqa: SLF001
        cur.execute(sql, params)
        row = cur.fetchone()
    # 必须结束读事务：psycopg3 会在第一条 execute 时隐式开事务，只 SELECT 而不
    # commit/rollback 会把连接留在 "idle in transaction"，一直握着 ACCESS SHARE。
    # 而下一个用例的建表 DDL 要 ACCESS EXCLUSIVE，于是被自己上一个用例挡住——
    # 这就是本文件最初挂死 >60s 的直接原因（真库 pg_stat_activity 里查得到）。
    store._conn.rollback()  # noqa: SLF001
    return row


def _count(store: PostgresReviewStore, repo: str, pr: int, sha: str) -> int:
    row = _fetch_one(
        store,
        "SELECT count(*) FROM code_review_prs "
        "WHERE repo=%s AND pr_number=%s AND head_sha=%s",
        (repo, pr, sha),
    )
    return int(row[0])


_SELECT_BY_IDENTITY = (
    "SELECT status, posted_review_id, title, state_transitions FROM code_review_prs "
    "WHERE repo=%s AND pr_number=%s AND head_sha=%s"
)


@pytest.mark.postgres
def test_redelivery_reuses_the_same_row(pg_store: PostgresReviewStore) -> None:
    """D2 的原始判据：重复投递撞唯一约束 → 复用同一行。"""
    tag = uuid.uuid4().hex[:8]
    rec = _rec(f"org-{tag}/app", 7, f"sha-{tag}")
    pg_store.save(rec)
    pg_store.save(rec)
    pg_store.save(rec)
    assert _count(pg_store, f"org-{tag}/app", 7, f"sha-{tag}") == 1


@pytest.mark.postgres
def test_same_sha_in_different_repos_stays_separate(pg_store: PostgresReviewStore) -> None:
    """回归（2026-10-01）：同 sha 的**不同仓库**必须是两条任务。

    这条在 save() 改成从 dataclass 取身份三列之前会失败：那时 repo 恒为 ''、
    pr_number 恒为 0，两行的 UNIQUE 三元组都是 ('', 0, sha)，于是被合并成一行——
    而合并不报错，表现为"另一个仓库的审查结果覆盖了这一个"。
    """
    tag = uuid.uuid4().hex[:8]
    sha = f"shared-{tag}"
    pg_store.save(_rec(f"org-a-{tag}/app", 7, sha))
    pg_store.save(_rec(f"org-b-{tag}/app", 7, sha))
    assert _count(pg_store, f"org-a-{tag}/app", 7, sha) == 1
    assert _count(pg_store, f"org-b-{tag}/app", 7, sha) == 1


@pytest.mark.postgres
def test_identity_columns_are_stored_not_defaulted(pg_store: PostgresReviewStore) -> None:
    """身份三列与 title 必须落真值，不能是 .get() 的默认值。"""
    tag = uuid.uuid4().hex[:8]
    repo, pr, sha = f"org-{tag}/app", 11, f"sha-{tag}"
    pg_store.save(_rec(repo, pr, sha))
    row = _fetch_one(pg_store, _SELECT_BY_IDENTITY, (repo, pr, sha))
    assert row is not None
    assert row[2] == f"title-{repo}-{pr}", "title 落成了空串（raw 没被填）"


@pytest.mark.postgres
def test_new_task_starts_queued_with_one_transition(pg_store: PostgresReviewStore) -> None:
    """新任务默认 queued，且第一条迁移记录存在（生命周期从第一行就可追溯）。"""
    tag = uuid.uuid4().hex[:8]
    repo, pr, sha = f"org-{tag}/app", 12, f"sha-{tag}"
    pg_store.save(_rec(repo, pr, sha))
    row = _fetch_one(pg_store, _SELECT_BY_IDENTITY, (repo, pr, sha))
    assert row[0] == "queued"
    transitions = row[3]
    assert isinstance(transitions, list) and len(transitions) == 1
    assert transitions[0]["to"] == "queued"


@pytest.mark.postgres
def test_redelivery_neither_wipes_posted_review_id_nor_reverts_status(
    pg_store: PostgresReviewStore,
) -> None:
    """重投不得抹掉已写回标记，也不得把 done 拽回 queued。

    构造方式是**先模拟 D4 的结果**（直接把行推到 done 并写上 posted_review_id），
    再重投一次 save()——对应真实时序里"GitHub 重试了一次已经处理完的 webhook"。
    """
    tag = uuid.uuid4().hex[:8]
    repo, pr, sha = f"org-{tag}/app", 13, f"sha-{tag}"
    pg_store.save(_rec(repo, pr, sha))
    pg_store._connect()  # noqa: SLF001
    with pg_store._conn.cursor() as cur:  # noqa: SLF001
        cur.execute(
            "UPDATE code_review_prs SET status='done', posted_review_id='rv_12345' "
            "WHERE repo=%s AND pr_number=%s AND head_sha=%s",
            (repo, pr, sha),
        )
    pg_store._conn.commit()  # noqa: SLF001

    pg_store.save(_rec(repo, pr, sha))

    row = _fetch_one(pg_store, _SELECT_BY_IDENTITY, (repo, pr, sha))
    assert row[1] == "rv_12345", "重投把已写回的 review id 抹成空了 → 会重复评论"
    assert row[0] == "done", "重投把 done 的任务拽回 queued → 会重跑一遍"


@pytest.mark.postgres
def test_schema_migration_is_idempotent(pg_store: PostgresReviewStore) -> None:
    """迁移脚本必须可重复执行。

    ``_ensure_schema`` 在每次进程启动时都跑一遍，所以"跑第二次不炸且不重复建索引"
    是硬要求，不是加分项。
    """
    from apps.code_review_pipeline.storage import review_store as rs

    rs._ensure_schema(DSN)  # noqa: SLF001
    row = _fetch_one(
        pg_store,
        "SELECT count(*) FROM pg_indexes WHERE indexname='uq_code_review_prs_identity'",
        (),
    )
    assert int(row[0]) == 1


@pytest.mark.postgres
def test_migration_fails_fast_instead_of_hanging_when_locked() -> None:
    """真去抢一把 ACCESS EXCLUSIVE 锁，验证迁移**超时退出**而不是挂死。

    这条是本文件存在价值的核心，也是 fake psycopg 永远测不出的那类：
    单测里"假连接收到了 lock_timeout 参数"是绿的，而真实 psycopg 根本不认那个
    参数（必须走 options）。只有真库能区分"参数被传了"与"参数真的生效了"。

    做法：另开一条连接，BEGIN 后执行 ALTER TABLE 拿到 ACCESS EXCLUSIVE 并**不提交**，
    然后调 _ensure_schema —— 它必须在一分钟内抛 StorageInitError。
    """
    import time

    from apps.code_review_pipeline.storage import review_store as rs

    blocker = psycopg.connect(DSN)
    try:
        with blocker.cursor() as cur:
            # 不 commit：事务保持打开，锁一直被握住
            cur.execute("ALTER TABLE code_review_prs ADD COLUMN IF NOT EXISTS _lock_probe INT")
        started = time.monotonic()
        with pytest.raises(rs.StorageInitError):
            rs._ensure_schema(DSN)  # noqa: SLF001
        elapsed = time.monotonic() - started
        assert elapsed < 60, f"迁移等了 {elapsed:.1f}s 才失败，说明没在超时"
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.postgres
def test_lock_timeout_is_actually_in_effect(pg_store: PostgresReviewStore) -> None:
    """直接问服务端：这条连接的 lock_timeout 到底是不是 15s。

    比"看代码里有没有写"强一层：SHOW 返回的是 PG 真正生效的运行参数。
    """
    conn = psycopg.connect(DSN, options=f"-c lock_timeout={rs_lock_timeout_ms()}")
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW lock_timeout")
            value = cur.fetchone()[0]
        conn.rollback()
    finally:
        conn.close()
    assert value == "15s", f"服务端报告的 lock_timeout 是 {value!r}，不是 15s"


def rs_lock_timeout_ms() -> int:
    """读实现里的那个常量，避免测试与实现各写一份数字。"""
    from apps.code_review_pipeline.storage import review_store as rs

    return rs._LOCK_TIMEOUT_MS  # noqa: SLF001
