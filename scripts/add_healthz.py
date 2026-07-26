with open('D:/HermesData/moa-gateway/app/main.py', 'r', encoding='utf-8-sig') as f:
    c = f.read()

endpoint = '''

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    checks = {}

    redis_check = "unknown"
    try:
        from app.redis_state.store import RedisConfig, RedisStateStore
        store = RedisStateStore(RedisConfig(url="redis://localhost:6379/0"))
        client = await store.connect()
        pong = await client.ping()
        if pong:
            redis_check = "fallback_memory" if store.is_fallback else "connected"
        await store.close()
    except Exception as e:
        redis_check = "error: " + str(e)[:50]
    checks["redis"] = redis_check

    healthy_values = {"connected", "ok", "healthy", "fallback_memory"}
    all_healthy = all(v in healthy_values for v in checks.values())
    return {"status": "healthy" if all_healthy else "degraded", "checks": checks}
'''

marker = '@app.get("/health")'
c = c.replace('@app.get("/health")\nasync def health() -> dict[str, str]:\n    return {"status": "ok", "version": "0.1.0"}',
              '@app.get("/health")\nasync def health() -> dict[str, str]:\n    return {"status": "ok", "version": "0.1.0"}\n\n' + endpoint, 1)

with open('D:/HermesData/moa-gateway/app/main.py', 'w', encoding='utf-8') as f:
    f.write(c)

import ast
ast.parse(c)
print("OK")
