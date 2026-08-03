import pathlib
root = pathlib.Path(__file__).resolve().parents[1] / 'docs' / 'adr'

adrs = {
    'adr-005-redis-ha.md': '''# ADR-005: Redis HA + Memory Fallback

**Status**: Accepted
**Date**: 2026-07-24

## Context

Redis is the state store for FSM session stacks and feature flags.
Single Redis = SPOF. Production needs HA.

## Decision

Connection priority: Sentinel -> Single Redis -> MemoryStateStore
- MemoryStateStore: thread-safe dict, same async interface
- Logs CRITICAL warning on fallback activation
- Config: sentinel_hosts, sentinel_master, enable_fallback

## Consequences

Zero Redis dependency for dev. Sentinel failover transparent.
Memory mode loses data on restart; no Lua locks in fallback.
''',
    'adr-006-privacy-erasure.md': '''# ADR-006: Privacy Erasure API

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
''',
    'adr-007-otel-config.md': '''# ADR-007: OTel Tracing Production Config

**Status**: Accepted
**Date**: 2026-07-25

## Context

OTel was initialized with hardcoded localhost:4317 endpoint,
causing noisy Transient error logs when no collector is running.

## Decision

- Default: OTLP disabled (empty endpoint)
- Enable via OTEL_EXPORTER_OTLP_ENDPOINT env var
- Fallback to ConsoleSpanExporter when endpoint is unreachable
- Removed unused span() async context manager

## Consequences

Clean logs by default. Tracing enabled on demand with one env var.
''',
}

for name, content in adrs.items():
    (root / name).write_text(content, encoding='utf-8')
    print(f'Written: {name}')
