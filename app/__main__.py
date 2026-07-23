from __future__ import annotations


def power_on_self_test() -> dict[str, object]:
    from app.config import settings

    return {
        "app": "moa-gateway",
        "version": "0.1.0",
        "env": settings.env,
        "redis_url_set": bool(settings.redis_url),
        "guard_default": settings.hitl_enabled,
    }
