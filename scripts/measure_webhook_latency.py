"""量 webhook 投递耗时到底花在哪（D5 收尾）。

为什么要量：计划里的判据是"``demo review`` <1s 返回 202"。实测在 Docker/Windows
 这台机器上冷请求 ~1.27s、热请求 ~0.99s，卡在 1s 边缘。与其把阈值从 1000 挪到
 1500 来"通过"，不如先说清楚这 1s 是什么：

- 冷请求的额外 ~280ms 是进程内**首次**建 store 时跑的那遍幂等 DDL；
- 热请求那 ~1s 里到底有多少在 Postgres、多少在 Redis，光看端到端数字猜不出来。

    uv run python scripts/measure_webhook_latency.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from apps.code_review_pipeline.routing.github_signature import sign_payload  # noqa: E402


def _payload(repo: str, pr: int, sha: str) -> bytes:
    return json.dumps(
        {
            "action": "opened",
            "pull_request": {
                "number": pr,
                "title": "latency probe",
                "user": {"login": "probe"},
                "head": {"sha": sha},
                "base": {"sha": "0" * 40},
            },
            "repository": {"full_name": repo},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def main() -> int:
    load_dotenv()
    secret = settings.github_webhook_secret
    if not secret:
        print("GITHUB_WEBHOOK_SECRET 未设置，无法投递", file=sys.stderr)
        return 2

    url = f"http://127.0.0.1:{settings.gateway_port}/webhook/github/review"

    # ── 端到端：5 个**不同** PR，避免第二个就撞幂等变成 200 ──────────────
    samples: list[int] = []
    with httpx.Client(timeout=30.0) as client:
        for i in range(5):
            raw = _payload("shing26/probe", 900 + i, f"{i:04d}" + "0" * 36)
            t = time.perf_counter()
            resp = client.post(
                url,
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sign_payload(raw, secret),
                },
            )
            ms = (time.perf_counter() - t) * 1000
            samples.append(ms)
            print(f"  投递 #{900 + i}: HTTP {resp.status_code}  {ms:.0f}ms")

    warm = samples[1:]
    print(f"\n端到端（含建 store / 落库 / 入队）")
    print(f"  冷请求(首条): {samples[0]:.0f}ms")
    print(f"  热请求中位数 : {statistics.median(warm):.0f}ms")

    # ── 分项：热路径上每一步各自多少 ────────────────────────────────────
    print("\n分项（热路径）")
    import redis as redis_mod

    r = redis_mod.Redis.from_url(settings.redis_url, decode_responses=False)
    t = time.perf_counter()
    r.ping()
    print(f"  redis ping        : {(time.perf_counter() - t) * 1000:.0f}ms")
    t = time.perf_counter()
    r.xadd("moa:tasks:probe", {"k": "v"})
    print(f"  redis XADD        : {(time.perf_counter() - t) * 1000:.0f}ms")
    r.delete("moa:tasks:probe")

    import psycopg

    dsn = settings.vector_db_dsn
    if dsn:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                t = time.perf_counter()
                cur.execute(
                    "SELECT status FROM code_review_prs "
                    "WHERE repo=%s AND pr_number=%s AND head_sha=%s",
                    ("shing26/probe", 900, "0" * 40),
                )
                cur.fetchone()
                print(f"  pg SELECT(身份)   : {(time.perf_counter() - t) * 1000:.0f}ms")
            conn.rollback()
    else:
        print("  pg: 未配置 DSN，跳过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

