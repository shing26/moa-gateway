from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


def _lock_key(session_id: str) -> str:
    return f"moa:lock:{session_id}"


def _stack_key(session_id: str) -> str:
    return f"moa:stack:{session_id}"


def _context_key(session_id: str, state: str) -> str:
    return f"moa:ctx:{session_id}:{state}"


async def push_state(redis: Any, session_id: str, state: str, context: dict[str, Any], ttl: int = 86400) -> None:
    key = _stack_key(session_id)
    ctx_key = _context_key(session_id, state)
    payload = {"state": state, "context_ref": ctx_key}
    await redis.lpush(key, __import__("json").dumps(payload))
    await redis.expire(key, ttl)
    await redis.hset(ctx_key, mapping={"data": __import__("json").dumps(context)})
    await redis.expire(ctx_key, ttl)


async def pop_state(redis: Any, session_id: str) -> dict[str, Any] | None:
    key = _stack_key(session_id)
    raw = await redis.lpop(key)
    if raw is None:
        return None
    payload = __import__("json").loads(raw)
    ctx_key = payload.get("context_ref")
    data = await redis.hget(ctx_key, "data")
    if data and ctx_key:
        await redis.delete(ctx_key)
    return payload


async def get_stack_depth(redis: Any, session_id: str) -> int:
    return int(await redis.llen(_stack_key(session_id)) or 0)


async def reset_stack(redis: Any, session_id: str) -> None:
    key = _stack_key(session_id)
    items = await redis.lrange(key, 0, -1)
    for raw in items:
        try:
            payload = __import__("json").loads(raw)
            ctx_key = payload.get("context_ref")
            if ctx_key:
                await redis.delete(ctx_key)
        except Exception:
            pass
    await redis.delete(key)
