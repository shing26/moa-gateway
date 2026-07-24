

## Resolution

**Decision**: Option B (Docker Compose + Cloud Redis)
- Local: Docker Compose with single redis:7-alpine
- Production: Cloud Redis (via env vars REDIS_URL / REDIS_SENTINEL_HOSTS)
- Hot-switch via environment variables
- MemoryStateStore fallback when Redis unavailable

Config: app/config.py -> redis_url/redis_sentinel_hosts
Code: app/redis_state/store.py (already supports Sentinel + single Redis + memory fallback)
Depends: docker-compose.dev.yml (already has redis service)
