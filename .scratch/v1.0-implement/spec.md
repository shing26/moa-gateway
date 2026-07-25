# v1.0 Production Hardening

**Status**: Spec
**Date**: 2026-07-25

## Problem Statement

MoA Engine v0.5 is feature-complete but not yet production-ready.
Missing CI/CD pipeline, audit log persistence, and production-grade
deployment automation. Wayfinder map identified clear remaining work.

## Solution

Production-hardening in three areas:

1. CI/CD: GitHub Actions build -> test -> deploy pipeline
2. Audit: AsyncWal persistence with log rotation
3. Config: .env based deployment with Docker Compose

## User Stories

1. As a developer, I want automated CI on every push, so that regressions are caught quickly
2. As a developer, I want Docker image build on every tag, so that deployment is one command
3. As an operator, I want audit logs persisted to files with rotation, so that PIPL compliance is met
4. As an operator, I want log retention configurable via env var, so that storage costs are controlled
5. As a developer, I want one-command local setup, so that onboarding is fast

## Implementation Decisions

- CI: GitHub Actions (ubuntu-latest, Python 3.12)
- Audit log file path: LOG_DIR env var (default: ./logs/)
- Log rotation: LOG_RETENTION_DAYS env var (default: 90)
- Docker build: on git tag push, push to ghcr.io
- No ES dependency for v1.0 MVP (AsyncWal + file fallback sufficient)

## Testing Decisions

- Existing 120+ unit tests as CI gate
- EsWriter tested with mock HTTP
- AsyncWal tested with in-memory buffer (existing tests)
- New: audit file rotation test

## Out of Scope

- Cloud deployment (k8s, multi-region)
- Embedding model hot-swap
- Multi-channel support (Discord/Slack)

## Further Notes

Wayfinder map at .scratch/v1.0-production/
ADR docs at docs/adr/
