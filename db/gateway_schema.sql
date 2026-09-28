-- ============================================================================
-- moa-gateway 统一检索 schema (P4)
--
-- 这张表是网关侧唯一的检索语料存储，替代 app/vectordb/__init__.py 里的进程内
-- dict。它同时承载两类数据：
--   1. 知识库分片（metadata.source = 'knowledge'）
--   2. 会话上下文片段（metadata.session_id = ...）
-- 二者靠 metadata 区分，不再各存一份。
--
-- 与 apps/code_review_pipeline/storage/schema.sql 的关系（冲突 X1 的裁决）：
--   那张表带 `trace_id REFERENCES code_review_prs(trace_id)` 外键，是 code review
--   流水线专用的。网关文档没有 PR 的概念，强行复用会被外键挡住，因此这里独立建表，
--   不做外键耦合。两边共用同一个 PostgreSQL 实例与 pgvector 扩展即可。
--
-- 执行方式：
--   - 若 VECTOR_DB_AUTO_MIGRATE=1（默认），PgVectorClient.start() 会按语句逐条执行本文件。
--   - 若托管 PG（RDS / Cloud SQL）不允许应用账号 CREATE EXTENSION，请由 DBA 预先执行
--     `CREATE EXTENSION IF NOT EXISTS vector;`，其余语句应用账号可自行完成。
--   语句以分号分隔，本文件内不含字符串字面量中的分号，可安全按分号切分。
--
-- 运行环境约束：
--   异步连接池需要 SelectorEventLoop。Linux（目标部署环境）默认即此；
--   Windows 下 uvicorn 也会默认切到 WindowsSelectorEventLoopPolicy，正常。
--   仅在用任意 io 默认 ProactorEventLoop 的测试/脚本里直接驱动 start() 才会
--   报 "Psycopg cannot use the ProactorEventLoop"，此时用
--   asyncio.SelectorEventLoop 即可，不影响生产路径。
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS gateway_documents (
    id          TEXT        PRIMARY KEY,
    content     TEXT        NOT NULL,
    metadata    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    embedding   vector(1536),
    -- 稀疏腿的预分词形式（ADR-020）：写入时由 app/vectordb/keywords.query_tokens
    -- 生成、空格分隔。**必须预分词**——PG 默认 FTS 不切中文，直接拿 content 建
    -- tsvector 会让中文查询静默返回空结果（不报错，只是查不到）。
    tokens      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 已存在的表加列：上面的 CREATE TABLE IF NOT EXISTS 对**已建好的表**不会补列，
-- 所以迁移必须显式写这一句，否则老库升级后 tokens 永远是 NULL、稀疏腿永远空。
ALTER TABLE gateway_documents ADD COLUMN IF NOT EXISTS tokens TEXT;

-- metadata 过滤对应 `metadata @> %s::jsonb`，GIN 是唯一能用上它的索引类型。
CREATE INDEX IF NOT EXISTS gateway_documents_metadata_idx
    ON gateway_documents USING gin (metadata jsonb_path_ops);

-- HNSW 而非 IVFFlat：IVFFlat 需要有代表性的数据才能聚类出有用的 lists，在刚建好
-- 的空表上召回率很差，且数据增长后必须 REINDEX。HNSW 可以从空表开始增量构建，
-- 更适合当前"先落地、后灌数据"的阶段。
-- 前置条件：pgvector >= 0.5.0。
-- 注：向量维度固定 1536，必须与 VECTOR_DB_EMBEDDING_DIM 一致；若更换 embedding
--    模型导致维度变化，需重建本列与索引。
CREATE INDEX IF NOT EXISTS gateway_documents_embedding_idx
    ON gateway_documents USING hnsw (embedding vector_cosine_ops);

-- 稀疏腿索引（ADR-020）。必须用 to_tsvector 的**两参形式**：带 regconfig 字面量时
-- 它是 IMMUTABLE，能进索引；单参形式是 STABLE，建不了表达式索引。
-- 配置用 'simple'（不做 stemming/停用词），因为列里存的已经是 bigram 词元——
-- 再词干化会把 bigram 拆坏。
CREATE INDEX IF NOT EXISTS gateway_documents_tokens_idx
    ON gateway_documents USING gin (to_tsvector('simple', tokens));
