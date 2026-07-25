# ADR-004: Audit Logging + VectorDB

**Status**: Accepted
**Date**: 2026-07-24

## Context

PIPL compliance requires audit trails for all agent interactions.
Sprint 3 added infrastructure for log persistence and semantic search.

## Decision

### Audit Log
- EsWriter: async bulk writer for Elasticsearch (NDJSON format)
- AsyncWal: local memory + disk buffer (1GB limit) as fallback
- WAL replays to ES when connectivity recovers
- Fields: trace_id, session_id, agent_name, intent, eval_score, guard_action

### VectorDB
- VectorDBClient: abstract in-memory implementation (Qdrant/Milvus ready)
- ContextRetriever: enriches AgentEnvelope.global_summary with historical context
- Keyword matching fallback when no embedding model is connected

## Consequences

Positive: No data loss during ES outages (WAL buffer). Right-to-erasure
delete_by_metadata on VectorDBClient.
Negative: ES and embedding model setup deferred (v1.0 scope). Default
keyword search has poor relevance vs real embeddings.
