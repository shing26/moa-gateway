from __future__ import annotations

import argparse
import asyncio
import os
import sys


def power_on_self_test() -> dict[str, object]:
    from app.config import settings

    return {
        "app": "moa-gateway",
        "version": "0.1.0",
        "env": settings.env,
        "redis_url_set": bool(settings.redis_url),
        "guard_default": settings.hitl_enabled,
    }


def _gateway_port() -> int:
    return int(os.getenv("GATEWAY_PORT") or os.getenv("APP_PORT") or "8081")


def run_server(
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
) -> None:
    import uvicorn

    config = uvicorn.Config(
        "app.main:app",
        host=host,
        port=port or _gateway_port(),
        loop="asyncio",
    )
    # Uvicorn 0.36+ selects ProactorEventLoop on Windows unless a custom
    # factory is supplied. psycopg's async pool rejects that loop.
    if sys.platform == "win32":
        config.get_loop_factory = lambda: asyncio.SelectorEventLoop
    uvicorn.Server(config).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Agent Gateway on a psycopg-safe loop.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    run_server(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
