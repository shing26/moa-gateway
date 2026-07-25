# ADR-003: Feature Flags + Prompt Registry + Canary

**Status**: Accepted
**Date**: 2026-07-24

## Context

Production deployment requires dynamic configuration without code deploys:
- Feature flags to toggle guard/evaluator behavior at runtime
- Versioned prompt management for A/B testing system prompts
- Canary traffic splitting for gradual rollouts

## Decision

### Feature Flags
- Dict-backed FeatureFlagClient with 5s TTL cache, env var fallback
- Flag naming: moa:flag:{name}
- Middleware injects FlagSnapshot into request.state.flags

### Prompt Registry
- Key pattern: prompt:{agent_name}:{version}
- Active pointer: prompt:{agent_name}:active
- Dict fallback for dev, Redis backing for production

### Canary
- session_id consistent hash for deterministic traffic split
- Configurable percentage (default 10%)
- Falls back to stable if canary version not found

## Consequences

Positive: Zero-downtime flag changes, prompt rollback in seconds.
Negative: Requires Redis for production flag/prompt storage.
