import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.redis_state.store import RedisConfig, RedisStateStore


async def check():
    cfg = RedisConfig(
        url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        sentinel_hosts=[],
        enable_fallback=True,
    )
    store = RedisStateStore(cfg)
    try:
        client = await store.connect()
        pong = await client.ping()
        mode = "memory" if store.is_fallback else "redis"
        print("Redis: OK" if pong else "Redis: FAIL")
        print("Mode:", mode)
    except Exception as e:
        print("Error:", e)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(check())
