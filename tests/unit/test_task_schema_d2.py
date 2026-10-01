"""D2：任务级幂等键与生命周期列（纯逻辑，不依赖真库）。

D2 的完成判据是"重复投递撞唯一约束 → 复用同一行"。这里只断言**SQL 形状**与
**迁移前提**，因为唯一约束的真实行为需要真 Postgres（Docker 未起时不可验证，
见计划文档）。

值得先说明一个反直觉的事实，它决定了去重步骤到底防什么：

``UNIQUE(repo, pr_number, head_sha)`` 在 trace_id 由这三元组拼出的前提下是
**冗余**的——同一三元组必然拼出同一个 trace_id，而 trace_id 是主键，所以三元组
不可能重复。那么去重在防什么？防的是**trace_id 拼接格式变更过的库**。本次 D1
就把 build_task_key 从"截断 12 hex"改成"完整 sha"，于是格式切换窗口里写下的
短 key 行，与之后写下的全 key 行，指向同一个 (repo, pr_number, head_sha) 但
trace_id 不同——正是唯一约束要拦的那种重复。

结论：去重不是可有可无的保险，而是"改过主键派生规则"这件事本身的必要代价。
这也正是保留 ``UNIQUE`` 的理由：它让业务身份在库里显式存在，
不再只能靠 trace_id 恰好同源这个约定。
"""

from __future__ import annotations

import re
from pathlib import Path

SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "code_review_pipeline"
    / "storage"
    / "schema.sql"
)
SQL = SCHEMA.read_text(encoding="utf-8")


def _statements() -> list[str]:
    return [s.strip() for s in SQL.split(";") if s.strip()]


def _strip_comments(stmt: str) -> str:
    """去掉块注释、行注释与字符串字面量，只留 SQL 结构本身。

    不做这一步的话，每条语句都以注释开头（``-- D2: ...``），``startswith("WITH ")``
    这类判断会全部落空——而且是静默落空：断言"期望 2 条实际 0 条"看着像 schema 写错，
    其实是测试自己没剥注释。
    """
    body = re.sub(r"/\*.*?\*/", " ", stmt, flags=re.DOTALL)
    body = re.sub(r"--[^\n]*", " ", body)
    body = re.sub(r"'(?:[^']|'')*'", "''", body)
    return body.strip()


def test_every_statement_is_structurally_balanced() -> None:
    """逐语句检查括号与引号闭合。

    没有 pglast / sqlglot 可用，也不打算为了"验证 SQL"给运行时加依赖。但完全不
    验等于把语法错误留到真库——而 D2 明确不做真库验证，所以这里用最廉价的结构
    检查兜住 gross error：CTE 少一个右括号、单引号没闭合这类肉眼易滑过去的错。

    注释与字符串里的括号/分号必须先剥掉，否则 '[]'::jsonb 和 DEFAULT '{}'::jsonb
    里的字符会被当成语法结构。
    """
    unbalanced: list[str] = []
    for stmt in _statements():
        body = _strip_comments(stmt)
        if body.count("(") != body.count(")"):
            unbalanced.append(stmt[:70])
    assert not unbalanced, f"括号不闭合的语句: {unbalanced}"


def test_dedupe_ctes_are_complete_wellformed_statements() -> None:
    """去重用的两条 WITH 语句必须以分号独立成句。

    ``_ensure_schema`` 把整个 schema.sql 一次性 ``cur.execute`` 过去。去重语句末尾
    若漏了分号，WITH 的 UPDATE 会和后面的 CREATE INDEX 粘成一句，语法直接报错。
    """
    with_statements = [
        s for s in map(_strip_comments, _statements()) if s.upper().startswith("WITH ")
    ]
    assert len(with_statements) == 2, (
        f"期望 2 条 WITH 语句（改挂 findings + 删除重复行），实际 {len(with_statements)}"
    )
    joined = " ".join(s.upper() for s in with_statements)
    assert "UPDATE CODE_REVIEW_FINDINGS" in joined
    assert "DELETE FROM CODE_REVIEW_PRS" in joined
    for stmt in with_statements:
        assert "PARTITION BY REPO, PR_NUMBER, HEAD_SHA" in stmt.upper(), (
            "去重窗口必须按业务身份分区，而不是按 trace_id"
        )


# ── 新增列 ──────────────────────────────────────────────────────────────


def test_schema_adds_posted_review_id_column() -> None:
    """posted_review_id 是 D4 幂等的主判据（写回是否已发生）。

    **必须可空且无默认值**：它是"还没有写回"的语义，写成 NOT NULL DEFAULT '' 就把
    "未写回"和"写回了空串"混成同一个值，幂等判断会在两者间来回横跳。
    """
    assert re.search(
        r"ADD COLUMN IF NOT EXISTS posted_review_id\s+TEXT", SQL
    ), "缺少 posted_review_id（且应为可空 TEXT）"


def test_schema_adds_state_transitions_jsonb_with_default() -> None:
    """state_transitions 记"这个任务走过哪些状态"。

    JSONB 而不是新建一张 transitions 表：审计链已经有独立的 jsonl 落地，
    这里要的是**跟着任务行走**的最近 N 次快照，能一条 SELECT 读出来。
    DEFAULT '[]' 是必须的——NOT NULL 列给不了 DEFAULT 就会让所有老行补列失败。
    """
    assert re.search(
        r"ADD COLUMN IF NOT EXISTS state_transitions\s+JSONB\s+NOT NULL DEFAULT '\[\]'::jsonb",
        SQL,
    ), "state_transitions 必须是 JSONB NOT NULL DEFAULT '[]'::jsonb"


def test_task_state_reuses_existing_status_column() -> None:
    """任务状态复用 ``status`` 列，**不新增 task_state**。

    查过全仓库：``status`` 只被写入（``save()`` 写死 'pending'），没有任何一处
    SELECT 或比较它——一列死数据。此时再 ADD COLUMN task_state 会让同一张表并存
    两套状态词汇，正是 ``app/models/errors.py`` 开头记下的教训（"三套词汇表并存"）。
    所以走 ALTER：改默认值 + 把遗留的 'pending' 归一。

    默认值也不能是 ``done`` 之类——那会让新投递的 PR 看起来已经审过，
    幂等逻辑直接短路返回。这是"默认值选错 = 静默丢任务"的典型。
    """
    assert "ADD COLUMN IF NOT EXISTS task_state" not in SQL, (
        "新增 task_state 会与既有 status 列形成两套状态词汇"
    )
    assert re.search(
        r"ALTER COLUMN status SET DEFAULT 'queued'", SQL
    ), "status 的默认值必须是 queued"
    assert re.search(
        r"UPDATE code_review_prs SET status = 'queued' WHERE status = 'pending'", SQL
    ), "遗留的 'pending' 必须归一，否则老行/新行并存两套词汇"


# ── 唯一约束 + 迁移前提 ─────────────────────────────────────────────────


def test_schema_declares_identity_unique_constraint() -> None:
    """幂等键 = (repo, pr_number, head_sha)，与 build_task_key 同源。

    这条约束才是 D4"重复 ``demo review 42`` 不新建任务"的落地点。注意它必须
    落在**完整 head_sha** 上，配合 build_task_key 不再截断——否则约束会先被
    trace_id 主键的冲突绕过，变成一条看着存在、实际不生效的唯一约束。
    """
    # 匹配索引定义而不是裸的 "UNIQUE (...)"：schema.sql 里 vectors 段已有一处
    # "UNIQUE INDEX IF NOT EXISTS"，宽泛的搜索会抓到不相干的旧索引（真踩过）。
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS uq_code_review_prs_identity\s+"
        r"ON code_review_prs\s*\(\s*repo\s*,\s*pr_number\s*,\s*head_sha\s*\)",
        SQL,
    ), "缺少 (repo, pr_number, head_sha) 唯一约束"


def test_schema_dedupes_before_creating_unique_index() -> None:
    """先去重、再建唯一索引，顺序不能反。

    D1 的 trace_id 派生规则刚从"截断 12 hex"改成"完整 sha"。库若跨过这个窗口，
    同一个 (repo, pr_number, head_sha) 会有两行、trace_id 各不相同，
    ``CREATE UNIQUE INDEX`` 直接报 duplicate key，迁移第一步就炸。
    去重规则：保留 updated_at 最新的一行（最后写入的那次最接近真相）。

    注意顺序还牵连外键，见下面那条测试。
    """
    sql_upper = SQL.upper()
    dedupe_at = sql_upper.find("DELETE FROM CODE_REVIEW_PRS")
    assert dedupe_at != -1, "缺少遗留重复行的去重步骤"

    index_at = sql_upper.find("CREATE UNIQUE INDEX IF NOT EXISTS UQ_CODE_REVIEW_PRS_IDENTITY")
    assert index_at != -1, "缺少 CREATE UNIQUE INDEX IF NOT EXISTS"
    assert dedupe_at < index_at, "去重必须在建唯一索引之前，否则老库上直接报错"


def test_schema_remaps_findings_before_deleting_duplicate_rows() -> None:
    """删除重复 prs 行之前，必须先把 findings 改挂到幸存行。

    ``code_review_findings.trace_id`` 有外键指向 ``code_review_prs``：直接删重复行
    会触发 FK violation，整条迁移回滚——即"迁移脚本在自己要修的数据上失败"。
    """
    upper = SQL.upper()
    remap_at = upper.find("UPDATE CODE_REVIEW_FINDINGS")
    dedupe_at = upper.find("DELETE FROM CODE_REVIEW_PRS")
    assert remap_at != -1, "缺少 findings 的 trace_id 改挂"
    assert remap_at < dedupe_at, "必须先改挂 findings 再删重复行，否则触发外键失败"


def test_schema_keeps_legacy_trace_id_primary_key() -> None:
    """trace_id 仍是 PK；唯一约束是并列的第二道身份，不改主键。

    防止后续"顺手优化"把主键换成自增 id：findings.trace_id 有 FK 指向本表，
    换主键会让所有已落库的 trace_id 与 findings 脱钩。
    """
    assert "trace_id TEXT PRIMARY KEY" in SQL


# ── 幂等写入语义（用假连接断言 SQL 形状）───────────────────────────────


class _RecordingCursor:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self._log = log

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self._log.append((sql, params))


class _RecordingConn:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self._log = log

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self._log)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _FakePsycopg:
    log: list[tuple[str, object]] = []

    @staticmethod
    def connect(dsn: str, **kwargs: object) -> _RecordingConn:
        return _RecordingConn(_FakePsycopg.log)


def _save_sql(monkeypatch) -> str:
    import sys

    from apps.code_review_pipeline.storage.review_store import (
        PostgresReviewStore,
        ReviewRecord,
    )

    _FakePsycopg.log = []
    monkeypatch.setitem(sys.modules, "psycopg", _FakePsycopg)
    store = PostgresReviewStore(dsn="postgresql://u:p@h:5432/db", _psycopg=_FakePsycopg)
    store.save(
        ReviewRecord(
            trace_id="cr_o/r#1@abc",
            repo="o/r",
            pr_number=1,
            head_sha="abc",
            author="u",
            findings_count=3,
            need_human_review=True,
            raw={"pr_number": 1, "head_sha": "abc"},
        )
    )
    return "\n".join(sql for sql, _ in _FakePsycopg.log)


def test_save_uses_identity_conflict_target_not_trace_id(monkeypatch) -> None:
    """插入冲突目标必须是**业务身份**，不是 trace_id。

    两者当前同源（trace_id 由 repo/pr/sha 拼出），但语义不同：trace_id 是
    "这条记录的地址"，(repo, pr_number, head_sha) 是"这件事本身"。幂等属于后者。
    若继续 ``ON CONFLICT (trace_id)``，一旦有人改 trace_id 格式，幂等就静默失效
    ——而 D2 加的唯一约束会成为一条永不生效的摆设。
    """
    sql = _save_sql(monkeypatch)
    assert re.search(r"ON CONFLICT\s*\(\s*repo\s*,\s*pr_number\s*,\s*head_sha\s*\)", sql), (
        "ON CONFLICT 仍以 trace_id 为目标：幂等语义错挂在地址上"
    )


def test_save_does_not_overwrite_posted_review_id_on_redelivery(monkeypatch) -> None:
    """重投**不得**把 posted_review_id 打回 NULL。

    这条是 D4 幂等的生死线：若重投时 `posted_review_id = EXCLUDED.posted_review_id`
    （EXCLUDED 恒为 NULL，因为入队时还没写回），那么**每次重投都会把"已写回"
    抹成"未写回"**，下一次投递就会在 GitHub 上再贴一条重复评论——
    恰好是 D4 要消灭的那个现象。
    """
    sql = _save_sql(monkeypatch)
    upsert = sql[sql.upper().find("ON CONFLICT"):]
    assert "posted_review_id = EXCLUDED.posted_review_id" not in upsert, (
        "重投会把已写回的 posted_review_id 抹掉，导致重复评论"
    )


def test_save_does_not_clobber_status_on_redelivery(monkeypatch) -> None:
    """重投不得把 task_state 打回 queued。

    同上：EXCLUDED.task_state 来自"刚入队的这条记录"，恒为 'queued'。如果无脑
    DO UPDATE，一个已经 done 的任务会被重投拽回 queued，下一轮派发再算一遍——
    计划里"done 不重算"的约束就此失效，且失效方式极安静（没有报错）。
    """
    sql = _save_sql(monkeypatch)
    upsert = sql[sql.upper().find("ON CONFLICT"):]
    assert "status = EXCLUDED.status" not in upsert, (
        "重投会把 done 的任务拽回 queued，违反'done 不重算'"
    )


def test_save_appends_state_transition_on_first_insert(monkeypatch) -> None:
    """首次插入必须写一条 queued 迁移，让任务的生命周期从第一行就有轨迹。

    同时锁住"必须用 Jsonb 包装"：psycopg3 会把 Python 的 list 适配成 PG 数组
    （oid 1005），而 state_transitions 是 jsonb 列。不显式包 Jsonb 的话，纯逻辑
    测试全绿，真库上第一次写入就报类型不匹配。
    """
    sql = _save_sql(monkeypatch)
    params = _FakePsycopg.log[0][1]
    assert isinstance(params, dict)
    raw = params["state_transitions"]
    assert type(raw).__name__ == "Jsonb", (
        f"state_transitions 传的是 {type(raw).__name__}，"
        "psycopg3 会把它适配成 PG array 而非 jsonb，真库上会报类型不匹配"
    )
    transitions = raw.obj
    assert isinstance(transitions, list) and len(transitions) == 1
    assert transitions[0]["to"] == "queued"
    assert transitions[0]["at"], "迁移记录缺时间戳，事后无法还原时序"
