from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover - psycopg 缺失时的降级（见 _missing_deps）
    # 没有 psycopg 时 build_review_store 早在选型阶段就抛 StorageInitError，这里
    # 只是让模块仍可 import（内存存储路径完全用不到 Jsonb）。
    def Jsonb(value: Any) -> Any:  # type: ignore[misc]
        return value

logger = logging.getLogger("moa.code_review.storage")

# 建表 / 业务连接的 TCP 超时（秒）。
#
# 为什么不写死 30：Windows 上"对不可达端口 SYN 无响应"走的是 OS 级重传，实测能到
# 分钟级（2026-10-01 实测 `import app.main` 挂死 >2.5 分钟且零日志）。libpq 不给
# connect_timeout 时**完全不看 socket 超时**，所以必须显式给。10s 足够覆盖同机
# docker-compose 的正常建连，又能让"库没起"在 10s 内变成一条明确异常。
_CONNECT_TIMEOUT_S = 10

# 迁移等锁的上限（毫秒）。取 15s：ACCESS EXCLUSIVE 在正常部署里应该"没有竞争地"
# 立刻拿到，多等十几秒基本意味着另一个进程正卡在同一次迁移上——早失败并给出明确
# 异常，远好过无声挂起。
_LOCK_TIMEOUT_MS = 15_000

# 任务状态的唯一词汇表。**只有这一份**——见 schema.sql 里"不新增 task_state"
# 的说明：同一张表并存两套状态词汇正是 app/models/errors.py 记下的教训。
# D2 只用到 queued；running/waiting_approval/posting/done/failed 随 D3 的状态机
# 一起加进来，不在这里预先声明未使用的值。
TASK_STATE_QUEUED = "queued"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StorageInitError(Exception):
    """Raised when the persistent store cannot be initialized."""


class TaskNotFound(LookupError):
    """任务行不存在。

    与 ``InvalidTaskTransition`` 分开：前者是"消息指向一个不存在的任务"（数据
    不一致，通常意味着入队时任务行没落库），后者是"任务在，但这一步不该发生"。
    两者在 worker 里的处置不同——前者要报警，后者多数是正常竞争的结果。
    """


def _missing_deps() -> list[str]:
    missing = []
    try:
        import psycopg  # noqa: F401
    except Exception:
        missing.append("psycopg")
    try:
        import pydantic  # noqa: F401
    except Exception:
        missing.append("pydantic")
    return missing


def _build_dsn() -> str | None:
    """DSN 与 ``app.config.settings`` 同源（同类分叉的第三例，2026-09-23）。

    此前这里读 ``CODE_REVIEW_DATABASE_URL / DATABASE_URL / POSTGRES_URL``，而 config
    读 ``VECTOR_DB_DSN / CODE_REVIEW_DATABASE_URL`` —— **只设 ``VECTOR_DB_DSN`` 的部署
    在网关侧"有库"，在这里却拿到 None 而静默回落非持久化**。现在 config 的链已收编
    三组名字，这里只做单点读取。
    """
    from app.config import settings

    return settings.vector_db_dsn or None


def _embedding_dim() -> int:
    """向量维度：与 ``app.config.settings`` 同源。

    回归（2026-09-23）：本函数与 ``rag/embeddings.py`` 的同名函数此前都**反向**
    优先 ``CODE_REVIEW_EMBEDDING_DIM``，而 ``app/config.py`` 优先
    ``VECTOR_DB_EMBEDDING_DIM`` —— 同一组 env 可能算出不同维度，而这个维度决定了
    建表 DDL 与写入向量的长度，错了直接失败。非正整数的 fail-fast 交给 config 层。
    """
    from app.config import settings

    dim = int(settings.vector_db_embedding_dim)
    if dim <= 0:
        raise StorageInitError(f"embedding dimension must be positive: {dim}")
    return dim


def render_schema(schema_sql: str, dim: int) -> str:
    """Bind the configured embedding dimension into the review pgvector DDL."""
    if dim <= 0:
        raise ValueError(f"embedding dimension must be positive: {dim}")
    return schema_sql.replace("vector(1536)", f"vector({dim})")


def _ensure_schema(dsn: str) -> None:
    import psycopg  # type: ignore[import-untyped]

    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as fh:
        schema_sql = render_schema(fh.read(), _embedding_dim())

    try:
        # lock_timeout 不是可选项：schema.sql 里有 ALTER TABLE 和非 CONCURRENTLY 的
        # CREATE UNIQUE INDEX，两者都要 ACCESS EXCLUSIVE 锁。任何一条在途的只读
        # 连接（哪怕只是一个没提交的 SELECT，它握 ACCESS SHARE）都能让这里无限期
        # 排队——PG 的锁等待默认没有超时，表现为"进程卡住"而非"迁移失败"，
        # 且日志上一片安静。
        #
        # 真库实测（2026-10-01）：两个连续跑的集成用例，第二个的建表被第一个用例
        # 遗留的 idle-in-transaction 连接挡住，pytest 挂死 >60s。
        # D3 之后 worker 与 gateway 是两个进程、都在启动时跑迁移，这个隐患从
        # "测试才会遇到"变成"生产启动就会遇到"。
        #
        # lock_timeout 必须走 `options` 而不是当成连接参数直传：它是 PG 的**服务端
        # 运行参数**，libpq 不认这个连接选项。真库实测直传会报
        # `invalid connection option "lock_timeout"`，整个迁移直接失败。
        # 讽刺的是这个错误是被"假连接的单测"漏掉的——假 psycopg 对任何 kwarg
        # 都点头，所以断言"传了 lock_timeout"在单测里是绿的。
        with psycopg.connect(
            dsn,
            connect_timeout=_CONNECT_TIMEOUT_S,
            options=f"-c lock_timeout={_LOCK_TIMEOUT_MS}",
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
                conn.commit()
    except Exception as exc:
        raise StorageInitError(f"failed to apply review schema: {exc}") from exc


def _create_pg_store(dsn: str) -> "PostgresReviewStore":
    try:
        import psycopg  # type: ignore[import-untyped]
    except Exception as exc:  # pragma: no cover
        raise StorageInitError(f"psycopg is required for Postgres store: {exc}") from exc
    return PostgresReviewStore(dsn=dsn, _psycopg=psycopg)


@dataclass(frozen=True)
class ReviewRecord:
    trace_id: str
    repo: str
    pr_number: int
    head_sha: str
    author: str
    findings_count: int
    need_human_review: bool
    raw: dict[str, Any]


class ReviewStore:
    def __init__(self) -> None:
        self._records: dict[str, ReviewRecord] = {}
        self._states: dict[str, str] = {}
        self._posted: dict[tuple[str, int, str], str] = {}

    def get(self, trace_id: str) -> ReviewRecord | None:
        return self._records.get(trace_id)

    # 任务状态方法的签名**必须与 PostgresReviewStore 完全一致**（都用
    # identity 三元组，不用 trace_id）。此前内存版按 trace_id、PG 版按 identity，
    # 于是 worker 在本地跑得通、接真库就崩——同一个坑的第五次变体：
    # 同一份契约在两个实现里各写一遍。

    def get_task_state(self, identity: tuple[str, int, str]) -> str | None:
        return self._states.get(identity)

    def _transition(
        self,
        identity: tuple[str, int, str],
        action: Any,
        allowed_from: tuple[str, ...],
    ) -> str:
        from apps.code_review_pipeline.task_state import (
            ACTION_TARGET,
            InvalidTaskTransition,
            TaskState,
        )

        target = ACTION_TARGET[action]
        current = self._states.get(identity)
        if current is None:
            raise TaskNotFound(f"no task for {identity!r}")
        if current not in allowed_from:
            raise InvalidTaskTransition(TaskState(current), action)
        self._states[identity] = target.value
        return target.value

    def claim_task(self, identity: tuple[str, int, str]) -> bool:
        from apps.code_review_pipeline.task_state import (
            InvalidTaskTransition,
            TaskAction,
            TaskState,
        )

        try:
            self._transition(identity, TaskAction.CLAIM, (TaskState.QUEUED.value,))
        except (TaskNotFound, InvalidTaskTransition):
            return False
        return True

    def acquire_task(self, identity: tuple[str, int, str], *, reclaim: bool = False) -> bool:
        from apps.code_review_pipeline.task_state import (
            InvalidTaskTransition,
            TaskAction,
            TaskState,
        )

        action = TaskAction.RESUME if reclaim else TaskAction.CLAIM
        allowed = (
            (TaskState.QUEUED.value, TaskState.RUNNING.value)
            if reclaim
            else (TaskState.QUEUED.value,)
        )
        try:
            self._transition(identity, action, allowed)
        except (TaskNotFound, InvalidTaskTransition):
            return False
        return True

    def transition_task(self, identity: tuple[str, int, str], action: str) -> str:
        from apps.code_review_pipeline.task_state import ALLOWED_ACTIONS, TaskAction

        act = TaskAction(action)
        allowed = tuple(sorted(s.value for s, acts in ALLOWED_ACTIONS.items() if act in acts))
        return self._transition(identity, act, allowed)

    def save(self, record: ReviewRecord) -> None:
        self._records[record.trace_id] = record
        identity = (record.repo, record.pr_number, record.head_sha)
        self._states.setdefault(identity, TASK_STATE_QUEUED)
        logger.info("review saved trace=%s findings=%d", record.trace_id, record.findings_count)

    async def close(self) -> None:
        self._records.clear()


class PostgresReviewStore:
    def __init__(self, dsn: str, _psycopg: Any | None = None) -> None:
        self._dsn = dsn
        self._psycopg = _psycopg
        self._conn = None

    def _connect(self):
        if self._conn is None:
            if self._psycopg is None:
                import psycopg  # type: ignore[import-untyped]
                self._psycopg = psycopg
            self._conn = self._psycopg.connect(
                self._dsn, connect_timeout=_CONNECT_TIMEOUT_S
            )

    def save(self, record: ReviewRecord) -> None:
        """写入/复用一条任务行（D2）。

        冲突目标是 **(repo, pr_number, head_sha)** 而不是 trace_id：后者是"这条记录
        的地址"，前者才是"这件事本身"。幂等属于后者——地址一变（trace_id 派生规则
        改过），挂在地址上的幂等会静默失效，而数据库里那条 UNIQUE 还在，看着好好的。

        DO UPDATE 的白名单是**刻意**的：只更新"这次投递能重新观察到的内容"
        （文件数、reviewers、是否需要人工、updated_at）。下面两类**不进白名单**：

        - ``posted_review_id``：EXCLUDED 里它恒为 NULL（入队时还没写回）。无脑
          DO UPDATE 会让**每次重投都把"已写回"抹成"未写回"**，下次投递就在 GitHub
          上再贴一条重复评论——恰好是 D4 要消灭的现象。
        - ``status``：EXCLUDED 里它恒为入队值。已 done 的任务被重投拽回 queued，
          下一轮派发会重算一遍，计划里"done 不重算"的约束就此失效，且失效方式
          极安静（不报错，只是多花一遍 5 个 agent）。
        """
        self._connect()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO code_review_prs (
                        trace_id, repo, pr_number, head_sha, base_sha, title, author, html_url, diff_url,
                        changed_files_count, labels, reviewers, overall_need_human_review, status,
                        state_transitions
                    ) VALUES (
                        %(trace_id)s, %(repo)s, %(pr_number)s, %(head_sha)s, %(base_sha)s, %(title)s,
                        %(author)s, %(html_url)s, %(diff_url)s, %(changed_files_count)s,
                        %(labels)s, %(reviewers)s, %(overall_need_human_review)s, %(status)s,
                        %(state_transitions)s
                    )
                    ON CONFLICT (repo, pr_number, head_sha) DO UPDATE SET
                        changed_files_count = EXCLUDED.changed_files_count,
                        reviewers = EXCLUDED.reviewers,
                        overall_need_human_review = EXCLUDED.overall_need_human_review,
                        updated_at = NOW()
                    """,
                    {
                        "trace_id": record.trace_id,
                        # 身份三列读 dataclass，不读 raw（2026-10-01）。
                        #
                        # 此前这里是 raw.get("repo", "")，而唯一的生产者
                        # github_review_route._record_from_result 传 raw={} —— 于是
                        # repo 恒为 ''、pr_number 恒为 0。真库里那行遗留数据就是
                        # 这么来的（trace_id 合法、repo 空、pr_number=0）。
                        # D2 刚把幂等键定成 UNIQUE(repo, pr_number, head_sha)：
                        # 建在 ('', 0, sha) 上的唯一约束，换个仓库的同一个 sha
                        # 就会误判成同一件事而合并。
                        #
                        # raw 里如果有值也不采信——它没有 schema、没有类型约定，
                        # 曾经就是那个"两处各写一遍"的第四处分叉。
                        "repo": record.repo,
                        "pr_number": int(record.pr_number or 0),
                        "head_sha": record.head_sha,
                        # 以下四列 dataclass 上没有对应字段（ReviewRecord 只保留了
                        # 幂等与统计所需的最小集），只能继续取 raw。NOT NULL 列写空串
                        # 不报错但会留下无意义行，所以生产方应尽量填满。
                        "base_sha": record.raw.get("base_sha", ""),
                        "title": record.raw.get("title", ""),
                        "author": record.author,
                        "html_url": record.raw.get("html_url", ""),
                        "diff_url": record.raw.get("diff_url", ""),
                        "changed_files_count": int(record.raw.get("changed_files_count", 0) or 0),
                        "labels": record.raw.get("labels", []),
                        "reviewers": record.raw.get("reviewers", []),
                        "overall_need_human_review": bool(record.need_human_review),
                        "status": TASK_STATE_QUEUED,
                        # psycopg3 会把 Python 的 list 适配成 PG **数组**（oid 1005），
                        # 而 state_transitions 是 jsonb 列——不显式包 Jsonb 的话，
                        # 真库上会报 column is of type jsonb but expression is of type
                        # array。这是纯逻辑测试测不出、只有真库才炸的那类错。
                        "state_transitions": Jsonb(
                            [{"from": None, "to": TASK_STATE_QUEUED, "at": _now_iso()}]
                        ),
                    },
                )
                self._conn.commit()
        except Exception:
            if self._conn:
                self._conn.rollback()
            raise

    def get(self, trace_id: str) -> ReviewRecord | None:
        self._connect()
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT trace_id, repo, pr_number, head_sha, author, changed_files_count, overall_need_human_review FROM code_review_prs WHERE trace_id = %s", (trace_id,))
                row = cur.fetchone()
                if not row:
                    return None
                trace_id, repo, pr_number, head_sha, author, changed_files_count, overall_need_human_review = row
                return ReviewRecord(
                    trace_id=trace_id,
                    repo=repo,
                    pr_number=int(pr_number or 0),
                    head_sha=head_sha or "",
                    author=author or "",
                    findings_count=int(changed_files_count or 0),
                    need_human_review=bool(overall_need_human_review),
                    raw={},
                )
        except Exception:
            return None

    # ── 任务状态（D3）────────────────────────────────────────────────────
    #
    # 全部用**带状态谓词的条件 UPDATE** 实现，不用"先 SELECT 再 UPDATE"。区别不是
    # 风格：两个 worker 同时收到同一条消息时（Streams 的至少一次投递，加上
    # XAUTOCLAIM 的重认领），先读后写会让两者都认为认领成功，于是 5 个 agent 跑
    # 两遍。条件 UPDATE 只有一行的 rowcount 为 1。rowcount==0 即"别人已拿走"，
    # 调用方据此跳过而不是重试。

    _IDENTITY = "repo=%s AND pr_number=%s AND head_sha=%s"

    def get_task_state(self, identity: tuple[str, int, str]) -> str | None:
        repo, pr_number, head_sha = identity
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM code_review_prs WHERE " + self._IDENTITY,
                (repo, pr_number, head_sha),
            )
            row = cur.fetchone()
        self._conn.rollback()
        return str(row[0]) if row else None

    def get_posted_review_id(self, identity: tuple[str, int, str]) -> str | None:
        """已写回的 GitHub review id；None = 还没写回（D4 幂等的主判据）。

        刻意读**身份三元组**而不是 trace_id：trace_id 是派生出来的地址，改一次
        格式就查不到历史记录；而幂等判据必须在任何派生规则变更后依然成立。
        """
        repo, pr_number, head_sha = identity
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT posted_review_id FROM code_review_prs WHERE " + self._IDENTITY,
                (repo, pr_number, head_sha),
            )
            row = cur.fetchone()
        self._conn.rollback()
        if not row or row[0] is None:
            return None
        return str(row[0])

    def set_posted_review_id(self, identity: tuple[str, int, str], review_id: str) -> None:
        repo, pr_number, head_sha = identity
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE code_review_prs SET posted_review_id = %s, updated_at = NOW() "
                "WHERE " + self._IDENTITY,
                (review_id, repo, pr_number, head_sha),
            )
        self._conn.commit()

    def _transition(
        self,
        identity: tuple[str, int, str],
        action: Any,
        allowed_from: tuple[str, ...],
    ) -> str:
        """把任务从 ``allowed_from`` 之一推进到 ``action`` 的目标状态。"""
        from apps.code_review_pipeline.task_state import (
            ACTION_TARGET,
            InvalidTaskTransition,
            TaskState,
        )

        target = ACTION_TARGET[action]
        repo, pr_number, head_sha = identity
        stamp = _now_iso()
        rows_in = []
        for src in allowed_from:
            rows_in.append({"from": src, "to": target.value, "at": stamp})
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE code_review_prs SET status = %s, "
                "state_transitions = state_transitions || %s::jsonb, updated_at = NOW() "
                "WHERE " + self._IDENTITY + " AND status = ANY(%s) RETURNING status",
                # 顺序必须与 SQL 里占位符的出现顺序一致：status、jsonb、
                # 然后是 _IDENTITY 的 repo/pr_number/head_sha，最后是 ANY。
                # 少传会在真库上直接报 "6 placeholders but 3 parameters"；
                # 假 cursor 不校验，于是单测全绿（同一个教训的又一次）。
                (
                    target.value,
                    Jsonb(rows_in),
                    repo,
                    pr_number,
                    head_sha,
                    list(allowed_from),
                ),
            )
            row = cur.fetchone()
            if row is not None:
                self._conn.commit()
                return str(row[0])
            cur.execute(
                "SELECT status FROM code_review_prs WHERE " + self._IDENTITY,
                (repo, pr_number, head_sha),
            )
            current = cur.fetchone()
        self._conn.rollback()
        if current is None:
            raise TaskNotFound(f"no task for {identity!r}")
        raise InvalidTaskTransition(TaskState(current[0]), action)

    def claim_task(self, identity: tuple[str, int, str]) -> bool:
        """认领一个 queued 任务；已被别人认领则返回 False（不抛）。"""
        from apps.code_review_pipeline.task_state import (
            InvalidTaskTransition,
            TaskAction,
            TaskState,
        )

        try:
            self._transition(identity, TaskAction.CLAIM, (TaskState.QUEUED.value,))
        except (TaskNotFound, InvalidTaskTransition):
            return False
        return True

    def acquire_task(self, identity: tuple[str, int, str], *, reclaim: bool = False) -> bool:
        """取得一个任务的所有权。``reclaim=True`` 时允许接管 running 的任务。

        存在的理由（2026-10-01，集成测试实测）：worker 崩在"认领之后、状态落库
        之前"时，任务行已经是 running、消息还在 PEL。只认 queued 的话，重启后的
        worker 认领失败 → 直接跳过 → 任务永久卡在 running，且没有任何报错。

        ``reclaim=True`` 只应来自 XAUTOCLAIM 路径：Redis 保证只有空闲超过
        min-idle-time 的消息才会被回收，所以"原 worker 还活着但很慢"与"原 worker
        死了"在那一刻无法区分。这里选择**当作它死了**（重跑一遍）而不是当作它活着
        （任务永久卡住）—— 前者浪费一次审查，后者丢失任务。
        """
        from apps.code_review_pipeline.task_state import (
            InvalidTaskTransition,
            TaskAction,
            TaskState,
        )

        action = TaskAction.RESUME if reclaim else TaskAction.CLAIM
        allowed = (
            (TaskState.QUEUED.value, TaskState.RUNNING.value)
            if reclaim
            else (TaskState.QUEUED.value,)
        )
        try:
            self._transition(identity, action, allowed)
        except (TaskNotFound, InvalidTaskTransition):
            return False
        return True

    def transition_task(self, identity: tuple[str, int, str], action: str) -> str:
        """按动作推进任务；当前状态不允许则抛 InvalidTaskTransition。"""
        from apps.code_review_pipeline.task_state import ALLOWED_ACTIONS, TaskAction

        act = TaskAction(action)
        allowed = tuple(sorted(s.value for s, acts in ALLOWED_ACTIONS.items() if act in acts))
        return self._transition(identity, act, allowed)

    def get_posted_review_id(self, identity: tuple[str, int, str]) -> str | None:
        """已写回的 GitHub review id；None = 还没写回（D4 幂等的主判据）。"""
        return self._posted.get(identity)

    def set_posted_review_id(self, identity: tuple[str, int, str], review_id: str) -> None:
        self._posted[identity] = review_id

    async def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def build_review_store() -> ReviewStore:
    """
    Build the review store based on environment config.

    Priority:
    1. Postgres if CODE_REVIEW_DATABASE_URL / DATABASE_URL / POSTGRES_URL is set
    2. In-memory fallback otherwise

    ``CODE_REVIEW_AUTO_MIGRATE=0`` 时跳过建表 DDL（2026-10-01）。

    为什么需要这个开关：D3 的 worker 是**独立进程**，它和 gateway 都会在启动时
    走到这里。迁移要 ACCESS EXCLUSIVE 锁，于是两个进程会互相排队；而一个只握
    读锁的在途连接就足以让迁移挂死（真库实测）。worker 只是个消费者——schema
    的所有权应该归 gateway / 显式迁移命令，不该由每个消费方在启动时改。
    """
    dsn = _build_dsn()
    if not dsn:
        logger.info("no database URL configured; using in-memory review store")
        return ReviewStore()

    from app.config import settings

    if not settings.code_review_auto_migrate:
        logger.info("CODE_REVIEW_AUTO_MIGRATE=0; skipping review schema migration")
        return PostgresReviewStore(dsn=dsn)

    missing = _missing_deps()
    if missing:
        raise StorageInitError(
            "database URL is configured, but required packages are missing: "
            + ", ".join(missing)
        )

    _ensure_schema(dsn)
    logger.info("using Postgres review store")
    return PostgresReviewStore(dsn=dsn)
