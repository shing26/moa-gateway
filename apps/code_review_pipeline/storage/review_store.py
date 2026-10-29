from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from apps.code_review_pipeline.schemas.feedback import (
    FINDING_STATUS_PUBLISHED,
    FeedbackStatus,
    FindingFeedback,
)

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


class StaleReviewTrace(RuntimeError):
    """A callback no longer points at the current review attempt for its PR."""


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
    review_run_id: str = ""


class ReviewStore:
    def __init__(self) -> None:
        self._records: dict[str, ReviewRecord] = {}
        self._states: dict[str, str] = {}
        self._posted: dict[tuple[str, int, str], str] = {}
        self._publish_leases: dict[tuple[str, int, str], tuple[str, float]] = {}
        self._findings: dict[str, tuple[Any, ...]] = {}
        self._finding_events: dict[str, dict[tuple[str, str, str], Any]] = {}
        self._feedback_events: dict[str, FindingFeedback] = {}
        self._latest_human_feedback: dict[tuple[str, str, str], FindingFeedback] = {}

    def get(self, trace_id: str) -> ReviewRecord | None:
        return self._records.get(trace_id)

    def current_trace_id(self, repo: str, pr_number: int) -> str | None:
        """Return the newest review attempt for a PR.

        The trace identity includes the head SHA. A callback from an older
        card must not be accepted just because its own task row still exists.
        """
        for trace_id in reversed(tuple(self._records)):
            record = self._records[trace_id]
            if record.repo == repo and record.pr_number == int(pr_number):
                return trace_id
        return None

    def _record_for_identity(
        self, identity: tuple[str, int, str]
    ) -> ReviewRecord | None:
        for record in reversed(tuple(self._records.values())):
            if (
                record.repo,
                record.pr_number,
                record.head_sha,
            ) == identity:
                return record
        return None

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
        *,
        expected_trace_id: str | None = None,
        require_current_trace: bool = False,
        expected_review_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        from apps.code_review_pipeline.task_state import (
            ACTION_TARGET,
            InvalidTaskTransition,
            TaskState,
        )

        if require_current_trace:
            current_trace = self.current_trace_id(identity[0], identity[1])
            if expected_trace_id is None or current_trace != expected_trace_id:
                raise StaleReviewTrace(
                    f"stale review trace: expected {expected_trace_id!r}, current {current_trace!r}"
                )
        target = ACTION_TARGET[action]
        current = self._states.get(identity)
        if current is None:
            raise TaskNotFound(f"no task for {identity!r}")
        if expected_review_run_id is not None:
            current_record = self._record_for_identity(identity)
            current_run_id = (
                current_record.review_run_id if current_record is not None else ""
            )
            if current_run_id != expected_review_run_id:
                raise StaleReviewTrace(
                    "stale review run: "
                    f"expected {expected_review_run_id!r}, current {current_run_id!r}"
                )
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

    def transition_task(
        self,
        identity: tuple[str, int, str],
        action: str,
        *,
        expected_trace_id: str | None = None,
        require_current_trace: bool = False,
        expected_review_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        from apps.code_review_pipeline.task_state import ALLOWED_ACTIONS, TaskAction

        act = TaskAction(action)
        allowed = tuple(sorted(s.value for s, acts in ALLOWED_ACTIONS.items() if act in acts))
        return self._transition(
            identity,
            act,
            allowed,
            expected_trace_id=expected_trace_id,
            require_current_trace=require_current_trace,
            expected_review_run_id=expected_review_run_id,
            metadata=metadata,
        )

    def get_posted_review_id(self, identity: tuple[str, int, str]) -> str | None:
        """已写回的 GitHub review id；None = 还没写回（D4 幂等的主判据）。"""
        return self._posted.get(identity)

    def start_review_run(
        self, identity: tuple[str, int, str], review_run_id: str
    ) -> None:
        if not review_run_id:
            raise ValueError("review_run_id is required")
        record = self._record_for_identity(identity)
        if record is None:
            raise TaskNotFound(f"no task for {identity!r}")
        self._records[record.trace_id] = replace(
            record, review_run_id=review_run_id
        )

    def get_review_run_id(self, trace_id: str) -> str | None:
        record = self._records.get(trace_id)
        return record.review_run_id if record is not None else None

    def set_posted_review_id(self, identity: tuple[str, int, str], review_id: str) -> None:
        self._posted[identity] = review_id
        self._publish_leases.pop(identity, None)

    def claim_publish_attempt(
        self,
        identity: tuple[str, int, str],
        *,
        owner: str,
        lease_seconds: int,
        expected_review_run_id: str | None = None,
    ) -> bool:
        """Claim the single cross-process GitHub write lease while in posting."""
        if lease_seconds <= 0 or not owner:
            raise ValueError("publish lease owner and positive duration are required")
        if self._states.get(identity) != "posting" or identity in self._posted:
            return False
        if expected_review_run_id is not None:
            record = self._record_for_identity(identity)
            if record is None or record.review_run_id != expected_review_run_id:
                return False
        current = self._publish_leases.get(identity)
        now = time.monotonic()
        if current is not None and current[0] != owner and current[1] > now:
            return False
        self._publish_leases[identity] = (owner, now + lease_seconds)
        return True

    def release_publish_attempt(
        self,
        identity: tuple[str, int, str],
        *,
        owner: str,
    ) -> None:
        current = self._publish_leases.get(identity)
        if current is not None and current[0] == owner:
            self._publish_leases.pop(identity, None)

    def save(self, record: ReviewRecord) -> None:
        existing = self._records.get(record.trace_id)
        if existing is not None and not record.review_run_id:
            record = replace(record, review_run_id=existing.review_run_id)
        self._records[record.trace_id] = record
        identity = (record.repo, record.pr_number, record.head_sha)
        self._states.setdefault(identity, TASK_STATE_QUEUED)
        logger.info("review saved trace=%s findings=%d", record.trace_id, record.findings_count)

    def save_review(self, record: ReviewRecord, findings: tuple[Any, ...]) -> None:
        snapshot = _dedupe_findings(findings, trace_id=record.trace_id)
        for finding in findings:
            events = self._finding_events.setdefault(record.trace_id, {})
            events.setdefault(_finding_event_key(finding), finding)
        self.save(record)
        self._findings[record.trace_id] = snapshot

    def list_findings(self, trace_id: str) -> tuple[Any, ...]:
        return self._findings.get(trace_id, ())

    def list_finding_events(self, trace_id: str) -> tuple[Any, ...]:
        """Return the append-only finding lineage in first-seen order."""
        return tuple(self._finding_events.get(trace_id, {}).values())

    def record_feedback(self, feedback: FindingFeedback) -> bool:
        if feedback.event_id in self._feedback_events:
            return False
        current_run_id = self.get_review_run_id(feedback.trace_id)
        if current_run_id is None or feedback.review_run_id != current_run_id:
            raise ValueError("feedback belongs to a stale review run")
        published = {
            str(getattr(item, "finding_uid", ""))
            for item in self.list_findings(feedback.trace_id)
            if str(getattr(item, "status", "")) == FINDING_STATUS_PUBLISHED
        }
        if feedback.finding_uid not in published:
            raise ValueError("feedback references a finding that is not published")
        self._feedback_events[feedback.event_id] = feedback
        if feedback.label_source == "human":
            current = self._latest_human_feedback.get(
                (feedback.trace_id, feedback.review_run_id, feedback.finding_uid)
            )
            if current is None or (
                (feedback.created_at or ""),
                feedback.event_id,
            ) >= (
                (current.created_at or ""),
                current.event_id,
            ):
                self._latest_human_feedback[
                    (feedback.trace_id, feedback.review_run_id, feedback.finding_uid)
                ] = feedback
        return True

    def feedback_status(self, trace_id: str) -> FeedbackStatus:
        published = tuple(
            str(getattr(item, "finding_uid", ""))
            for item in self.list_findings(trace_id)
            if str(getattr(item, "status", "")) == FINDING_STATUS_PUBLISHED
        )
        latest = {
            finding_uid: feedback
            for finding_uid in published
            if (
                feedback := self._latest_human_feedback.get(
                    (trace_id, self.get_review_run_id(trace_id) or "", finding_uid)
                )
            )
            is not None
        }
        missing = tuple(item for item in published if item not in latest)
        return FeedbackStatus(
            trace_id=trace_id,
            total_findings=len(published),
            labelled_findings=len(latest),
            missing_finding_uids=missing,
            latest_human_by_finding=latest,
        )

    async def close(self) -> None:
        self._records.clear()
        self._findings.clear()
        self._finding_events.clear()
        self._feedback_events.clear()
        self._latest_human_feedback.clear()
        self._publish_leases.clear()


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

    @staticmethod
    def _record_params(record: ReviewRecord) -> dict[str, Any]:
        return {
            "trace_id": record.trace_id,
            "repo": record.repo,
            "pr_number": int(record.pr_number or 0),
            "head_sha": record.head_sha,
            "base_sha": record.raw.get("base_sha", ""),
            "title": record.raw.get("title", ""),
            "author": record.author,
            "html_url": record.raw.get("html_url", ""),
            "diff_url": record.raw.get("diff_url", ""),
            "changed_files_count": int(record.raw.get("changed_files_count", 0) or 0),
            "labels": record.raw.get("labels", []),
            "reviewers": record.raw.get("reviewers", []),
            "overall_need_human_review": bool(record.need_human_review),
            "review_metrics": Jsonb(record.raw.get("critic_metrics", {})),
            "review_run_id": record.review_run_id,
            "status": TASK_STATE_QUEUED,
            "state_transitions": Jsonb(
                [{"from": None, "to": TASK_STATE_QUEUED, "at": _now_iso()}]
            ),
        }

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
                        changed_files_count, labels, reviewers, overall_need_human_review,
                        review_metrics, review_run_id, status, state_transitions
                    ) VALUES (
                        %(trace_id)s, %(repo)s, %(pr_number)s, %(head_sha)s, %(base_sha)s, %(title)s,
                        %(author)s, %(html_url)s, %(diff_url)s, %(changed_files_count)s,
                        %(labels)s, %(reviewers)s, %(overall_need_human_review)s,
                        %(review_metrics)s, %(review_run_id)s, %(status)s, %(state_transitions)s
                    )
                    ON CONFLICT (repo, pr_number, head_sha) DO UPDATE SET
                        changed_files_count = EXCLUDED.changed_files_count,
                        reviewers = EXCLUDED.reviewers,
                        overall_need_human_review = EXCLUDED.overall_need_human_review,
                        review_metrics = CASE
                            WHEN EXCLUDED.review_metrics = '{}'::jsonb
                                THEN code_review_prs.review_metrics
                            ELSE EXCLUDED.review_metrics
                        END,
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
                        "review_metrics": Jsonb(record.raw.get("critic_metrics", {})),
                        "review_run_id": record.review_run_id,
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

    def save_review(self, record: ReviewRecord, findings: tuple[Any, ...]) -> None:
        """Atomically upsert task state and replace the current finding snapshot."""
        for finding in findings:
            if str(getattr(finding, "trace_id", "")) != record.trace_id:
                raise ValueError("all findings must belong to the review trace")
        finding_events = findings
        findings = _dedupe_findings(findings, trace_id=record.trace_id)
        self._connect()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO code_review_prs (
                        trace_id, repo, pr_number, head_sha, base_sha, title, author, html_url, diff_url,
                        changed_files_count, labels, reviewers, overall_need_human_review,
                        review_metrics, review_run_id, status, state_transitions
                    ) VALUES (
                        %(trace_id)s, %(repo)s, %(pr_number)s, %(head_sha)s, %(base_sha)s, %(title)s,
                        %(author)s, %(html_url)s, %(diff_url)s, %(changed_files_count)s,
                        %(labels)s, %(reviewers)s, %(overall_need_human_review)s,
                        %(review_metrics)s, %(review_run_id)s, %(status)s, %(state_transitions)s
                    )
                    ON CONFLICT (repo, pr_number, head_sha) DO UPDATE SET
                        changed_files_count = EXCLUDED.changed_files_count,
                        reviewers = EXCLUDED.reviewers,
                        overall_need_human_review = EXCLUDED.overall_need_human_review,
                        review_metrics = CASE
                            WHEN EXCLUDED.review_metrics = '{}'::jsonb
                                THEN code_review_prs.review_metrics
                            ELSE EXCLUDED.review_metrics
                        END,
                        review_run_id = CASE
                            WHEN EXCLUDED.review_run_id = ''
                                THEN code_review_prs.review_run_id
                            ELSE EXCLUDED.review_run_id
                        END,
                        updated_at = NOW()
                    """,
                    self._record_params(record),
                )
                for finding in finding_events:
                    cur.execute(
                        """
                        INSERT INTO code_review_finding_events (
                            trace_id, finding_uid, fingerprint, agent, finding_id,
                            status, revision_of, severity, category, file, line,
                            title, description, suggestion, confidence, team_specific,
                            evidence, evidence_verified, source_agents, critic_decision, critic_reason
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (trace_id, finding_uid, status, critic_decision)
                        DO NOTHING
                        """,
                        (
                            finding.trace_id,
                            finding.finding_uid,
                            finding.fingerprint,
                            finding.agent,
                            finding.raw_finding_id,
                            finding.status,
                            finding.revision_of,
                            finding.severity,
                            finding.category,
                            finding.file,
                            finding.line,
                            finding.title,
                            finding.description,
                            finding.suggestion,
                            finding.confidence,
                            finding.team_specific,
                            list(finding.evidence),
                            bool(finding.evidence_verified),
                            list(finding.source_agents),
                            finding.critic_decision,
                            finding.critic_reason,
                        ),
                    )
                # Feedback is append-only. A reclaim may rewrite the finding
                # snapshot, but it must never erase who labelled what; events
                # are joined back by finding_uid and later events win.
                cur.execute(
                    "DELETE FROM code_review_findings WHERE trace_id = %s",
                    (record.trace_id,),
                )
                for finding in findings:
                    cur.execute(
                        """
                        INSERT INTO code_review_findings (
                            trace_id, agent, finding_id, severity, category, file, line,
                            title, description, suggestion, confidence, team_specific, evidence,
                            evidence_verified, finding_uid, fingerprint, status, revision_of, source_agents,
                            critic_decision, critic_reason
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            finding.trace_id,
                            finding.agent,
                            finding.raw_finding_id,
                            finding.severity,
                            finding.category,
                            finding.file,
                            finding.line,
                            finding.title,
                            finding.description,
                            finding.suggestion,
                            finding.confidence,
                            finding.team_specific,
                            list(finding.evidence),
                            bool(finding.evidence_verified),
                            finding.finding_uid,
                            finding.fingerprint,
                            finding.status,
                            finding.revision_of,
                            list(finding.source_agents),
                            finding.critic_decision,
                            finding.critic_reason,
                        ),
                    )
            self._conn.commit()
        except Exception:
            if self._conn:
                self._conn.rollback()
            raise

    def list_findings(self, trace_id: str) -> tuple[Any, ...]:
        from apps.code_review_pipeline.schemas.feedback import PersistedFinding

        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT finding_uid, fingerprint, trace_id, agent, finding_id, status,
                       revision_of, severity, category, file, line, title, description,
                       suggestion, confidence, team_specific, evidence, source_agents,
                       critic_decision, critic_reason, evidence_verified
                FROM code_review_findings
                WHERE trace_id = %s
                ORDER BY id
                """,
                (trace_id,),
            )
            rows = cur.fetchall()
        self._conn.rollback()
        return tuple(
            PersistedFinding(
                finding_uid=row[0],
                fingerprint=row[1],
                trace_id=row[2],
                agent=row[3],
                raw_finding_id=row[4],
                status=row[5],
                revision_of=row[6],
                severity=row[7],
                category=row[8],
                file=row[9],
                line=int(row[10]),
                title=row[11],
                description=row[12],
                suggestion=row[13],
                confidence=float(row[14]),
                team_specific=bool(row[15]),
                evidence=tuple(row[16] or ()),
                source_agents=tuple(row[17] or ()),
                critic_decision=row[18] or "",
                critic_reason=row[19] or "",
                evidence_verified=bool(row[20]),
            )
            for row in rows
        )

    def list_finding_events(self, trace_id: str) -> tuple[Any, ...]:
        from apps.code_review_pipeline.schemas.feedback import PersistedFinding

        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT finding_uid, fingerprint, trace_id, agent, finding_id, status,
                       revision_of, severity, category, file, line, title, description,
                       suggestion, confidence, team_specific, evidence, source_agents,
                       critic_decision, critic_reason, evidence_verified
                FROM code_review_finding_events
                WHERE trace_id = %s
                ORDER BY event_id
                """,
                (trace_id,),
            )
            rows = cur.fetchall()
        self._conn.rollback()
        return tuple(
            PersistedFinding(
                finding_uid=row[0],
                fingerprint=row[1],
                trace_id=row[2],
                agent=row[3],
                raw_finding_id=row[4],
                status=row[5],
                revision_of=row[6],
                severity=row[7],
                category=row[8],
                file=row[9],
                line=int(row[10]),
                title=row[11],
                description=row[12],
                suggestion=row[13],
                confidence=float(row[14]),
                team_specific=bool(row[15]),
                evidence=tuple(row[16] or ()),
                source_agents=tuple(row[17] or ()),
                critic_decision=row[18] or "",
                critic_reason=row[19] or "",
                evidence_verified=bool(row[20]),
            )
            for row in rows
        )

    def record_feedback(self, feedback: FindingFeedback) -> bool:
        self._connect()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO code_review_finding_feedback (
                        event_id, trace_id, finding_uid, disposition, reason_code,
                        reason_note, actor, label_source, review_run_id, created_at
                    )
                    SELECT
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(NULLIF(%s, '')::timestamptz, NOW())
                    FROM code_review_findings f
                    JOIN code_review_prs p ON p.trace_id = f.trace_id
                    WHERE f.trace_id = %s
                      AND f.finding_uid = %s
                      AND f.status = %s
                      AND p.review_run_id = %s
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        feedback.event_id,
                        feedback.trace_id,
                        feedback.finding_uid,
                        feedback.disposition,
                        feedback.reason_code,
                        feedback.reason_note,
                        feedback.actor,
                        feedback.label_source,
                        feedback.review_run_id,
                        feedback.created_at,
                        feedback.trace_id,
                        feedback.finding_uid,
                        FINDING_STATUS_PUBLISHED,
                        feedback.review_run_id,
                    ),
                )
                inserted = cur.fetchone() is not None
                if not inserted:
                    cur.execute(
                        "SELECT 1 FROM code_review_finding_feedback WHERE event_id = %s",
                        (feedback.event_id,),
                    )
                    if cur.fetchone() is None:
                        raise ValueError(
                            "feedback references a stale or unpublished finding"
                        )
            self._conn.commit()
            return inserted
        except Exception:
            if self._conn:
                self._conn.rollback()
            raise

    def feedback_status(self, trace_id: str) -> FeedbackStatus:
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT f.finding_uid, p.review_run_id
                FROM code_review_findings f
                JOIN code_review_prs p ON p.trace_id = f.trace_id
                WHERE f.trace_id = %s AND f.status = %s
                ORDER BY f.id
                """,
                (trace_id, FINDING_STATUS_PUBLISHED),
            )
            published_rows = cur.fetchall()
            published = tuple(str(row[0]) for row in published_rows)
            review_run_id = str(published_rows[0][1]) if published_rows else ""
            if not published_rows:
                cur.execute(
                    "SELECT review_run_id FROM code_review_prs WHERE trace_id = %s",
                    (trace_id,),
                )
                run_row = cur.fetchone()
                review_run_id = str(run_row[0] or "") if run_row is not None else ""
            cur.execute(
                """
                SELECT DISTINCT ON (finding_uid)
                       event_id, trace_id, finding_uid, disposition, reason_code,
                       reason_note, actor, label_source, created_at
                FROM code_review_finding_feedback
                WHERE trace_id = %s AND label_source = 'human'
                  AND review_run_id = %s
                ORDER BY finding_uid, created_at DESC, event_id DESC
                """,
                (trace_id, review_run_id or ""),
            )
            events = cur.fetchall()
        self._conn.rollback()
        latest = {
            str(row[2]): FindingFeedback(
                event_id=row[0],
                trace_id=row[1],
                finding_uid=row[2],
                disposition=row[3],
                reason_code=row[4],
                reason_note=row[5],
                actor=row[6],
                label_source=row[7],
                created_at=row[8].isoformat() if row[8] is not None else "",
                review_run_id=review_run_id or "",
            )
            for row in events
            if str(row[2]) in published
        }
        missing = tuple(item for item in published if item not in latest)
        return FeedbackStatus(
            trace_id=trace_id,
            total_findings=len(published),
            labelled_findings=len(latest),
            missing_finding_uids=missing,
            latest_human_by_finding=latest,
        )

    def get(self, trace_id: str) -> ReviewRecord | None:
        self._connect()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT trace_id, repo, pr_number, head_sha, author, "
                    "changed_files_count, overall_need_human_review, review_run_id "
                    "FROM code_review_prs WHERE trace_id = %s",
                    (trace_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                (
                    trace_id,
                    repo,
                    pr_number,
                    head_sha,
                    author,
                    changed_files_count,
                    overall_need_human_review,
                    review_run_id,
                ) = row
                return ReviewRecord(
                    trace_id=trace_id,
                    repo=repo,
                    pr_number=int(pr_number or 0),
                    head_sha=head_sha or "",
                    author=author or "",
                    findings_count=int(changed_files_count or 0),
                    need_human_review=bool(overall_need_human_review),
                    raw={},
                    review_run_id=review_run_id or "",
                )
        except Exception:
            return None

    def current_trace_id(self, repo: str, pr_number: int) -> str | None:
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT trace_id FROM code_review_prs "
                "WHERE repo = %s AND pr_number = %s "
                "ORDER BY attempt_id DESC LIMIT 1",
                (repo, int(pr_number)),
            )
            row = cur.fetchone()
        self._conn.rollback()
        return str(row[0]) if row else None

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

    def start_review_run(
        self, identity: tuple[str, int, str], review_run_id: str
    ) -> None:
        if not review_run_id:
            raise ValueError("review_run_id is required")
        repo, pr_number, head_sha = identity
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE code_review_prs SET review_run_id = %s, updated_at = NOW() "
                "WHERE " + self._IDENTITY,
                (review_run_id, repo, pr_number, head_sha),
            )
            if cur.rowcount != 1:
                self._conn.rollback()
                raise TaskNotFound(f"no task for {identity!r}")
        self._conn.commit()

    def get_review_run_id(self, trace_id: str) -> str | None:
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT review_run_id FROM code_review_prs WHERE trace_id = %s",
                (trace_id,),
            )
            row = cur.fetchone()
        self._conn.rollback()
        return None if row is None else str(row[0] or "")

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

    def claim_publish_attempt(
        self,
        identity: tuple[str, int, str],
        *,
        owner: str,
        lease_seconds: int,
        expected_review_run_id: str | None = None,
    ) -> bool:
        if lease_seconds <= 0 or not owner:
            raise ValueError("publish lease owner and positive duration are required")
        repo, pr_number, head_sha = identity
        run_predicate = ""
        params: list[Any] = [
            owner,
            lease_seconds,
            owner,
            repo,
            pr_number,
            head_sha,
        ]
        if expected_review_run_id is not None:
            run_predicate = " AND review_run_id = %s"
            params.append(expected_review_run_id)
        self._connect()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE code_review_prs SET posting_owner = %s, "
                    "posting_lease_until = NOW() + make_interval(secs => %s), "
                    "updated_at = NOW() "
                    "WHERE status = 'posting' AND posted_review_id IS NULL "
                    "AND (posting_lease_until IS NULL OR posting_lease_until < NOW() "
                    "OR posting_owner = %s) "
                    "AND " + self._IDENTITY + run_predicate + " RETURNING trace_id",
                    tuple(params),
                )
                claimed = cur.fetchone() is not None
            self._conn.commit()
            return claimed
        except Exception:
            if self._conn:
                self._conn.rollback()
            raise

    def release_publish_attempt(
        self,
        identity: tuple[str, int, str],
        *,
        owner: str,
    ) -> None:
        repo, pr_number, head_sha = identity
        self._connect()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE code_review_prs SET posting_owner = NULL, "
                    "posting_lease_until = NULL, updated_at = NOW() "
                    "WHERE " + self._IDENTITY + " AND posting_owner = %s",
                    (repo, pr_number, head_sha, owner),
                )
            self._conn.commit()
        except Exception:
            if self._conn:
                self._conn.rollback()
            raise

    def _transition(
        self,
        identity: tuple[str, int, str],
        action: Any,
        allowed_from: tuple[str, ...],
        *,
        expected_trace_id: str | None = None,
        require_current_trace: bool = False,
        expected_review_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
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
        transition_detail = dict(metadata or {})
        rows_in = []
        for src in allowed_from:
            rows_in.append(
                {
                    "from": src,
                    "to": target.value,
                    "at": stamp,
                    **transition_detail,
                }
            )
        trace_predicate = ""
        trace_params: tuple[Any, ...] = ()
        if require_current_trace:
            trace_predicate = (
                " AND trace_id = ("
                "SELECT current.trace_id FROM code_review_prs current "
                "WHERE current.repo = %s AND current.pr_number = %s "
                "ORDER BY current.attempt_id DESC LIMIT 1)"
            )
            trace_params = (repo, pr_number)
        run_predicate = ""
        run_params: tuple[Any, ...] = ()
        if expected_review_run_id is not None:
            run_predicate = " AND review_run_id = %s"
            run_params = (expected_review_run_id,)
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE code_review_prs SET status = %s, "
                "state_transitions = state_transitions || %s::jsonb, updated_at = NOW() "
                "WHERE " + self._IDENTITY + " AND status = ANY(%s)"
                + trace_predicate
                + run_predicate
                + " RETURNING status",
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
                    *trace_params,
                    *run_params,
                ),
            )
            row = cur.fetchone()
            if row is not None:
                self._conn.commit()
                return str(row[0])
            if require_current_trace or expected_review_run_id is not None:
                cur.execute(
                    "SELECT trace_id, review_run_id FROM code_review_prs "
                    "WHERE repo = %s AND pr_number = %s "
                    "ORDER BY attempt_id DESC LIMIT 1",
                    (repo, pr_number),
                )
                current_trace = cur.fetchone()
                if require_current_trace and (
                    current_trace is None
                    or expected_trace_id is None
                    or str(current_trace[0]) != expected_trace_id
                ):
                    self._conn.rollback()
                    raise StaleReviewTrace(
                        "stale review trace: "
                        f"expected {expected_trace_id!r}, "
                        f"current {current_trace[0] if current_trace else None!r}"
                    )
                if (
                    expected_review_run_id is not None
                    and current_trace is not None
                    and str(current_trace[1] or "") != expected_review_run_id
                ):
                    self._conn.rollback()
                    raise StaleReviewTrace(
                        "stale review run: "
                        f"expected {expected_review_run_id!r}, "
                        f"current {current_trace[1]!r}"
                    )
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

    def transition_task(
        self,
        identity: tuple[str, int, str],
        action: str,
        *,
        expected_trace_id: str | None = None,
        require_current_trace: bool = False,
        expected_review_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """按动作推进任务；当前状态不允许则抛 InvalidTaskTransition。"""
        from apps.code_review_pipeline.task_state import ALLOWED_ACTIONS, TaskAction

        act = TaskAction(action)
        allowed = tuple(sorted(s.value for s, acts in ALLOWED_ACTIONS.items() if act in acts))
        return self._transition(
            identity,
            act,
            allowed,
            expected_trace_id=expected_trace_id,
            require_current_trace=require_current_trace,
            expected_review_run_id=expected_review_run_id,
            metadata=metadata,
        )

    async def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def _dedupe_findings(findings: tuple[Any, ...], *, trace_id: str) -> tuple[Any, ...]:
    """Keep one row per stable finding identity within a review snapshot.

    Re-claiming a task can regenerate the same semantic finding. PostgreSQL's
    unique key is deliberately finding_uid, so letting duplicates reach the
    INSERT loop would make an otherwise valid review fail. The later value wins
    so a final ``published`` state replaces an earlier candidate row.
    """
    by_uid: dict[str, Any] = {}
    for finding in findings:
        if str(getattr(finding, "trace_id", "")) != trace_id:
            raise ValueError("all findings must belong to the review trace")
        uid = str(getattr(finding, "finding_uid", ""))
        if not uid:
            raise ValueError("finding_uid is required")
        by_uid[uid] = finding
    return tuple(by_uid.values())


def _finding_event_key(finding: Any) -> tuple[str, str, str]:
    return (
        str(getattr(finding, "finding_uid", "")),
        str(getattr(finding, "status", "")),
        str(getattr(finding, "critic_decision", "")),
    )


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
