# ADR-006: Privacy Erasure API

**Status**: Accepted
**Date**: 2026-07-25

## Context

PIPL requires right to erasure. Users must be able to request
deletion of all personal data stored by the system.

## Decision

- DELETE /api/v1/privacy/user/{user_id}
- VectorDB: deletes all docs with matching user_id metadata
- Redis: best-effort (no user_id index on sessions)
- ES: requires configured ES instance (deferred)
- Server in China satisfies data localization requirement

## Consequences

Compliance with PIPL right to erasure. Data can be deleted
without restarting the service. No user_id -> session mapping
means some residual data may remain in Redis.
