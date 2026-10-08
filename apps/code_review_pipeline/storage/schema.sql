-- Code Review Pipeline Schema
-- Requires PostgreSQL 14+ for pgvector

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS code_review_prs (
    trace_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    html_url TEXT NOT NULL,
    diff_url TEXT NOT NULL,
    changed_files_count INTEGER NOT NULL DEFAULT 0,
    labels TEXT[] NOT NULL DEFAULT '{}',
    reviewers TEXT[] NOT NULL DEFAULT '{}',
    overall_need_human_review BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS code_review_findings (
    id SERIAL PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES code_review_prs(trace_id),
    agent TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    file TEXT NOT NULL,
    line INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    suggestion TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    team_specific BOOLEAN NOT NULL DEFAULT FALSE,
    evidence TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS code_review_vectors (
    id SERIAL PRIMARY KEY,
    trace_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1536),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Knowledge-base vectors use a non-PR trace_id such as "knowledge-base".
-- Drop the legacy FK for databases created by earlier schema versions.
ALTER TABLE code_review_vectors
    DROP CONSTRAINT IF EXISTS code_review_vectors_trace_id_fkey;

DELETE FROM code_review_vectors older
USING code_review_vectors newer
WHERE older.id > newer.id
  AND older.trace_id = newer.trace_id
  AND older.source_id = newer.source_id;

CREATE UNIQUE INDEX IF NOT EXISTS idx_code_review_vectors_trace_source
    ON code_review_vectors (trace_id, source_id);

CREATE INDEX IF NOT EXISTS idx_code_review_prs_repo ON code_review_prs (repo);
CREATE INDEX IF NOT EXISTS idx_code_review_prs_author ON code_review_prs (author);
CREATE INDEX IF NOT EXISTS idx_code_review_findings_trace ON code_review_findings (trace_id);
CREATE INDEX IF NOT EXISTS idx_code_review_findings_severity ON code_review_findings (severity);
CREATE INDEX IF NOT EXISTS idx_code_review_vectors_source ON code_review_vectors (source_type, source_id);

-- ── D2: 任务级幂等键与生命周期 (2026-10-01) ─────────────────────────────
--
-- 这三行 ADD COLUMN 不能省：CREATE TABLE IF NOT EXISTS 对**已建好的表**不补列。
-- 老库升级时不写它们，posted_review_id / task_state 永远不存在，而读写它们的代码
-- 在真库上才报 undefined_column —— 内存存储路径测不出来的那种错。
--
-- posted_review_id 刻意可空且无默认值：NULL = 还没写回。给 DEFAULT '' 会把
-- "未写回"和"写回了空串"合并成同一个值，D4 的幂等判断会在两者之间横跳。
ALTER TABLE code_review_prs ADD COLUMN IF NOT EXISTS posted_review_id TEXT;
ALTER TABLE code_review_prs ADD COLUMN IF NOT EXISTS state_transitions JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 任务状态复用既有的 status 列，**不新增 task_state**。
--
-- 原本打算加一列 task_state（queued/running/...），但查过全仓库：status 列
-- 只被写入、从不被读取（save() 里写死 'pending'，没有一处 SELECT 或比较它）。
-- 也就是说它是一列死数据。此时再加 task_state 就是同一张表上两套状态词汇——
-- 正是 app/models/errors.py 开头记录的那个教训（"三套词汇表并存"）。
-- 所以收编 status：改默认值，废掉 'pending' 这个只写不读的遗留值。
ALTER TABLE code_review_prs ALTER COLUMN status SET DEFAULT 'queued';

-- 'pending' 在老库里真实存在（已被写入过的行就是它）。统一成 queued，否则
-- 同一批任务会并存两套词汇：老行 pending / 新行 queued。
UPDATE code_review_prs SET status = 'queued' WHERE status = 'pending';

-- 幂等键 = (repo, pr_number, head_sha)，即"这份代码该被审一次"。
--
-- 为什么它现在**不是冗余约束**：trace_id 由这三元组拼出，trace_id 又是主键，
-- 所以理论上三元组不可能重复——除非 trace_id 的派生规则变过。本次 D1 恰好就改了
-- （build_task_key 从截断 12 hex 改成完整 sha）。库若跨过这个窗口，同一个三元组
-- 会有两行、trace_id 各不相同，唯一约束正好拦住。保留它也意味着**业务身份在库里
-- 显式存在**，不再只靠"trace_id 恰好同源"这个口头约定。
--
-- 去重必须在建索引之前：上面那个窗口已经足以让 CREATE UNIQUE INDEX 报 duplicate
-- key，迁移第一步就炸。幸存者取 updated_at 最新（最后写入最接近真相）。
-- WITH 一次算出重复组，DELETE / UPDATE 共用同一套窗口定义，避免两份逻辑漂移。
WITH dupes AS (
    SELECT trace_id,
           FIRST_VALUE(trace_id) OVER (
               PARTITION BY repo, pr_number, head_sha
               ORDER BY updated_at DESC, trace_id
           ) AS survivor,
           ROW_NUMBER() OVER (
               PARTITION BY repo, pr_number, head_sha
               ORDER BY updated_at DESC, trace_id
           ) AS rn
    FROM code_review_prs
)
UPDATE code_review_findings f
SET trace_id = d.survivor
FROM dupes d
WHERE f.trace_id = d.trace_id AND d.rn > 1;

-- UPDATE 必须排在 DELETE 之前：code_review_findings.trace_id 有外键指向本表，
-- 先删会让迁移整条回滚——即"迁移脚本在自己要修的数据上失败"。
WITH dupes AS (
    SELECT trace_id,
           ROW_NUMBER() OVER (
               PARTITION BY repo, pr_number, head_sha
               ORDER BY updated_at DESC, trace_id
           ) AS rn
    FROM code_review_prs
)
DELETE FROM code_review_prs
WHERE trace_id IN (SELECT trace_id FROM dupes WHERE rn > 1);

CREATE UNIQUE INDEX IF NOT EXISTS uq_code_review_prs_identity
    ON code_review_prs (repo, pr_number, head_sha);

-- D3 的 worker 按状态捞活（只查非终态任务），不必全表扫。
CREATE INDEX IF NOT EXISTS idx_code_review_prs_status ON code_review_prs (status);
-- 任务状态复用既有的 status 列，**不新增 task_state**。
--
-- 原本打算加一列 task_state（queued/running/...），但查过全仓库：status 列
-- 只被写入、从不被读取（save() 里写死 'pending'，没有一处 SELECT 或比较它）。
-- 也就是说它是一列死数据。此时再加 task_state 就是同一张表上两套状态词汇——
-- 正是 app/models/errors.py 开头记录的那个教训（"三套词汇表并存"）。
-- 所以收编 status：改默认值，删掉 'pending' 这个只写不读的遗留值。
--
-- SET DEFAULT 而不是加 NOT NULL：新列的 DEFAULT 是给老行用的，而 status 早已是
-- NOT NULL，无需再声明。
ALTER TABLE code_review_prs ALTER COLUMN status SET DEFAULT 'queued';

-- 'pending' 这个遗留值在老库里真实存在（有行已经被写过 pending）。统一成 queued
-- 否则同一批任务里会并存两套词汇：老行 pending / 新行 queued。
UPDATE code_review_prs SET status = 'queued' WHERE status = 'pending';

-- ── ADR-0021: 合并通道 (2026-10-08) ─────────────────────────────────────
--
-- 与 code_review_prs **分表**：两条状态线不同（见 merge_state.py 的模块注释），
-- 共用一张表会让 status 列变成两套词汇的杂糅——本文件上面已经记录过那次教训。
--
-- 主键是 (repo, pr_number, head_sha) 三元组，与 code_review_prs 的幂等键同构。
-- **sha 变了就是另一条记录**：审批是在某个 sha 上做出的，新 commit 到来意味着那张
-- 审批已经作废，不能续用。
CREATE TABLE IF NOT EXISTS pr_merge_gate (
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'watching',
    ci_state TEXT NOT NULL DEFAULT '',
    ci_failing JSONB NOT NULL DEFAULT '[]'::jsonb,
    card_message_id TEXT NOT NULL DEFAULT '',
    approver TEXT NOT NULL DEFAULT '',
    merged_sha TEXT NOT NULL DEFAULT '',
    failure_reason TEXT NOT NULL DEFAULT '',
    transitions JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (repo, pr_number, head_sha)
);

-- 轮询每轮都按 state 捞非终态记录，不必全表扫。
CREATE INDEX IF NOT EXISTS idx_pr_merge_gate_state ON pr_merge_gate (state);
