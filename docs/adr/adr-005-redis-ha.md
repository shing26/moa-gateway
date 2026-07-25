# ADR-005: Redis HA + Memory Fallback

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
