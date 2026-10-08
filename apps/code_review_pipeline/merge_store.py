"""合并通道的持久化（ADR-0021）。

形状照搬 ``review_store``：一个内存实现 + 一个 Postgres 实现，**两者签名必须完全
一致**。这条规矩是踩出来的——内存版按 trace_id、PG 版按 identity 的那次，worker
在本地跑得通、接真库就崩。同一个契约写两遍，迟早不一致。

与审查任务表分开而不是共用：两条状态线不同（见 ``merge_state`` 的模块注释），
共用一张表会让 ``status`` 列变成两套词汇的杂糅——``schema.sql`` 里已经记录过
"status 只写不读、又冒出 task_state"的那次教训。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from apps.code_review_pipeline.merge_state import (
    MergeAction,
    MergeState,
    is_terminal,
    next_state,
)
from apps.code_review_pipeline.storage.review_store import (
    Jsonb,
    _CONNECT_TIMEOUT_S,
)

logger = logging.getLogger("moa.merge_store")


@dataclass(frozen=True)
class MergeRecord:
    """一个 PR 在合并通道里的全部状态。

    身份是 ``(repo, pr_number, head_sha)`` 三元组，与 ``code_review_prs`` 的
    ``uq_code_review_prs_identity`` 同构。**sha 变了就是另一条记录**：审批是在某个
    sha 上做出的，新 commit 到来意味着那张审批已经作废，不能续用。
    """

    repo: str
    pr_number: int
    head_sha: str
    state: str = MergeState.WATCHING.value
    # CI 结论的原始取值（success/failure/pending/none），用于卡片文案与日志。
    ci_state: str = ""
    # 失败的 check：``[{"name": ..., "url": ...}]``。红的时候卡片要能说出"哪一条"。
    ci_failing: tuple[dict[str, str], ...] = ()
    # 飞书卡片的消息 id。作废卡片时要靠它去改那条消息，而不是再发一条。
    card_message_id: str = ""
    # 谁批的。审计链的入口，也是"这动作是人授权的"唯一凭据。
    approver: str = ""
    merged_sha: str = ""
    # 合并失败的原因（GitHub 的原话）。终态时它是人唯一能看到的解释。
    failure_reason: str = ""
    # 状态迁移历史，仿 ``state_transitions``。
    transitions: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def identity(self) -> tuple[str, int, str]:
        return (self.repo, self.pr_number, self.head_sha)

    @property
    def task_key(self) -> str:
        """与审查侧同构的稳定身份：``owner/repo#123@完整sha``。"""
        return f"{self.repo}#{self.pr_number}@{self.head_sha}"

    @property
    def is_terminal(self) -> bool:
        return is_terminal(MergeState(self.state))


class MergeNotFound(LookupError):
    """该 identity 不在通道里。"""


def _record_transition(
    record: MergeRecord, action: MergeAction, at: str, **updates: Any
) -> MergeRecord:
    """算目标状态、追加迁移历史、应用字段更新。**两个实现共用这一份**。

    共用是刻意的：状态机是纯逻辑，没有任何理由在两个存储实现里各写一遍——
    那正是"同一个契约写两遍"的老坑。
    """
    current = MergeState(record.state)
    target = next_state(current, action)
    entry = {"from": current.value, "to": target.value, "action": action.value, "at": at}
    return replace(
        record,
        state=target.value,
        transitions=(*record.transitions, entry),
        **updates,
    )


class MergeStore:
    """内存实现。生产走 Postgres，这个给单测和离线 demo 用。"""

    def __init__(self) -> None:
        self._records: dict[tuple[str, int, str], MergeRecord] = {}

    def save(self, record: MergeRecord) -> None:
        self._records[record.identity] = record

    def get(self, identity: tuple[str, int, str]) -> MergeRecord | None:
        return self._records.get(identity)

    def transition(
        self, identity: tuple[str, int, str], action: MergeAction, *, at: str, **updates: Any
    ) -> MergeRecord:
        record = self._records.get(identity)
        if record is None:
            raise MergeNotFound(f"no merge record for {identity!r}")
        updated = _record_transition(record, action, at, **updates)
        self._records[identity] = updated
        logger.info(
            "merge %s: %s -> %s (%s)",
            record.task_key,
            record.state,
            updated.state,
            action.value,
        )
        return updated

    def list_open(self) -> list[MergeRecord]:
        """所有非终态记录，给轮询用。

        顺序按 ``(repo, pr_number)`` 固定：轮询每一轮都应该以同样的顺序看同一批
        PR，否则日志里同一件事的行序每次不同，排查时无法比对。
        """
        return sorted(
            (r for r in self._records.values() if not r.is_terminal),
            key=lambda r: (r.repo, r.pr_number, r.head_sha),
        )


class PostgresMergeStore:
    """Postgres 实现。生产走这条。

    迁移用**条件 UPDATE** 而不是"读出来改完再写回去"：并发下两个进程同时看到
    ``watching``、各自算出 ``awaiting_approval`` 并先后写入，后写的那次会覆盖前一次
    的迁移历史（``transitions`` 是整列替换，不是追加）。条件 UPDATE 让第二次写入
    影响 0 行，于是"有人抢先了"变成一个能看见的事实，而不是静默丢失。
    """

    _IDENTITY = "repo = %s AND pr_number = %s AND head_sha = %s"

    # 允许通过 ``transition`` 更新的列。**白名单是硬约束**：SQL 里的列名由它拼出，
    # 不做白名单就等于让调用方决定 SQL 结构。
    _UPDATABLE = {
        "ci_state": "ci_state",
        "ci_failing": "ci_failing",
        "card_message_id": "card_message_id",
        "approver": "approver",
        "merged_sha": "merged_sha",
        "failure_reason": "failure_reason",
    }

    def __init__(self, dsn: str, _psycopg: Any | None = None) -> None:
        self._dsn = dsn
        self._psycopg = _psycopg
        self._conn = None

    def _connect(self) -> None:
        if self._conn is None:
            if self._psycopg is None:
                import psycopg  # type: ignore[import-untyped]

                self._psycopg = psycopg
            self._conn = self._psycopg.connect(
                self._dsn, connect_timeout=_CONNECT_TIMEOUT_S
            )

    def save(self, record: MergeRecord) -> None:
        """写入一条记录；已存在则**只更新 updated_at**。

        冲突时不动 ``state`` / ``ci_state`` 等任何业务列是刻意的：重投（webhook 重试、
        轮询重复）不该把一条已经推进到 ``awaiting_approval`` 的记录拽回 ``watching``。
        那正是 review_store 里 "DO UPDATE 白名单" 那条教训的另一处应用。
        """
        self._connect()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pr_merge_gate "
                    "(repo, pr_number, head_sha, state, transitions) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb) "
                    "ON CONFLICT (repo, pr_number, head_sha) DO UPDATE SET "
                    "updated_at = NOW()",
                    (
                        record.repo,
                        int(record.pr_number),
                        record.head_sha,
                        record.state,
                        Jsonb(list(record.transitions)),
                    ),
                )
                self._conn.commit()
        except Exception:
            if self._conn:
                self._conn.rollback()
            raise

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> MergeRecord:
        (
            repo,
            pr_number,
            head_sha,
            state,
            ci_state,
            ci_failing,
            card_message_id,
            approver,
            merged_sha,
            failure_reason,
            transitions,
        ) = row
        return MergeRecord(
            repo=str(repo),
            pr_number=int(pr_number),
            head_sha=str(head_sha),
            state=str(state),
            ci_state=str(ci_state or ""),
            ci_failing=tuple(dict(x) for x in (ci_failing or ())),
            card_message_id=str(card_message_id or ""),
            approver=str(approver or ""),
            merged_sha=str(merged_sha or ""),
            failure_reason=str(failure_reason or ""),
            transitions=tuple(dict(t) for t in (transitions or ())),
        )

    _COLUMNS = (
        "repo, pr_number, head_sha, state, ci_state, ci_failing, "
        "card_message_id, approver, merged_sha, failure_reason, transitions"
    )

    def get(self, identity: tuple[str, int, str]) -> MergeRecord | None:
        self._connect()
        repo, pr_number, head_sha = identity
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._COLUMNS} FROM pr_merge_gate WHERE " + self._IDENTITY,
                (repo, pr_number, head_sha),
            )
            row = cur.fetchone()
        return None if row is None else self._row_to_record(row)

    def transition(
        self,
        identity: tuple[str, int, str],
        action: MergeAction,
        *,
        at: str,
        **updates: Any,
    ) -> MergeRecord:
        unknown = set(updates) - set(self._UPDATABLE)
        if unknown:
            raise ValueError(f"cannot update columns: {sorted(unknown)}")

        record = self.get(identity)
        if record is None:
            raise MergeNotFound(f"no merge record for {identity!r}")
        # 先算目标状态：非法转移在这里就抛，一行 SQL 都不发。
        updated = _record_transition(record, action, at, **updates)

        sets = ["state = %s", "transitions = %s::jsonb", "updated_at = NOW()"]
        params: list[Any] = [updated.state, Jsonb(list(updated.transitions))]
        for key, value in updates.items():
            sets.append(f"{self._UPDATABLE[key]} = %s")
            params.append(Jsonb(list(value)) if key == "ci_failing" else value)
        # 乐观锁：只有状态还是我读到的那一刻，这次写入才生效。
        params.extend([record.repo, record.pr_number, record.head_sha, record.state])

        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE pr_merge_gate SET {', '.join(sets)} "
                f"WHERE {self._IDENTITY} AND state = %s RETURNING state",
                tuple(params),
            )
            row = cur.fetchone()
        if row is None:
            # 有人抢先改了。重新读一次，报出**当前真实状态**而不是我读到的那份。
            self._conn.rollback()
            current = self.get(identity)
            if current is None:
                raise MergeNotFound(f"no merge record for {identity!r}")
            raise InvalidMergeTransition(MergeState(current.state), action)
        self._conn.commit()
        logger.info(
            "merge %s: %s -> %s (%s)",
            record.task_key,
            record.state,
            updated.state,
            action.value,
        )
        return updated

    def list_open(self) -> list[MergeRecord]:
        """非终态记录，给轮询用。终态集合从状态机推导，不在这里另写一份。"""
        terminal = [s.value for s in MergeState if is_terminal(s)]
        self._connect()
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._COLUMNS} FROM pr_merge_gate "
                "WHERE NOT (state = ANY(%s)) ORDER BY repo, pr_number, head_sha",
                (terminal,),
            )
            rows = cur.fetchall()
        return [self._row_to_record(r) for r in rows]

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def build_merge_store() -> MergeStore | PostgresMergeStore:
    """按配置选实现。与 ``build_review_store`` 同一策略：有 DSN 走 PG，否则内存。

    **不做建表 DDL**：schema 的所有权归显式迁移（见 ``build_review_store`` 的
    docstring——worker 与 gateway 同时启动时，两边都改 schema 会互相等
    ACCESS EXCLUSIVE 锁，实测能挂死）。本通道的表由 gateway 启动时的迁移建。
    """
    from apps.code_review_pipeline.storage.review_store import _build_dsn

    dsn = _build_dsn()
    if not dsn:
        logger.info("no database URL configured; using in-memory merge store")
        return MergeStore()
    return PostgresMergeStore(dsn=dsn)
