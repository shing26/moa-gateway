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
