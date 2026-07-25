# ADR-007: OTel Tracing Production Config

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
