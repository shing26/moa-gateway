# Ticket 1: CI/CD + Docker Build

**Type**: implement
**Priority**: High
**Blocked by**: -

## Task

1. Add Docker build + publish step to .github/workflows/ci.yml
2. Build on every tag push (git tag v*)
3. Push to ghcr.io/shing26/moa-gateway
4. Smoke test: build image locally and verify health endpoint

## Acceptance Criteria

- git tag v0.1.0 && git push --tags triggers Docker build
- Image published to ghcr.io
- docker run starts gateway, /health returns 200
