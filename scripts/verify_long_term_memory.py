"""长期记忆生命周期真实路径验证：抽取 -> 写入 -> 跨会话召回 -> 冲突更新 -> 遗忘。

用法（默认读 .env；配置了 VECTOR_DB_DSN 即走 pgvector，否则走进程内存）：
    .venv/Scripts/python.exe scripts/verify_long_term_memory.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv()

from app.long_term_memory import LongTermMemory  # noqa: E402
from app.vectordb import build_vector_client  # noqa: E402


async def main() -> int:
    client = build_vector_client()
    backend = getattr(client, "backend", "?")
    print(f"[0] 向量后端: {backend}")

    await client.start()
    degraded = getattr(client, "_degraded_reason", None)
    if degraded:
        print(f"    FAIL: 后端启动失败并降级: {degraded}")
        return 1

    memory = LongTermMemory(client)
    user = f"verify-{int(time.time())}"
    other = f"{user}-other"
    failures: list[str] = []

    try:
        # 1) session A 写入，session B 召回（跨会话是长期记忆与短期记忆的分界）。
        applied = await memory.apply(user, "记住我的名字是小张", session_id="verify-session-a")
        print(f"[1] session A 写入: {applied}")
        if applied != ["remembered:name"]:
            failures.append("写入未产生 remembered:name")

        recalled = await memory.recall_context("我的名字是什么", user)
        print(f"[2] session B 召回: {recalled!r}")
        if "小张" not in recalled:
            failures.append("跨会话未召回上一会话写入的事实")

        # 2) 同一槽位再次写入是覆盖（冲突更新），不是追加。
        await memory.apply(user, "记住我的名字是老王", session_id="verify-session-c")
        docs = await memory.list_for(user)
        recalled = await memory.recall_context("我的名字是什么", user)
        print(f"[3] 覆盖后条目数={len(docs)} 召回={recalled!r}")
        if len(docs) != 1:
            failures.append(f"同槽位应为 1 条，实际 {len(docs)} 条")
        if "老王" not in recalled or "小张" in recalled:
            failures.append("覆盖后仍能召回旧值")

        # 3) 不同用户之间不可见。
        other_recalled = await memory.recall_context("我的名字是什么", other)
        print(f"[4] 其他用户召回: {other_recalled!r}")
        if other_recalled:
            failures.append("用户之间发生记忆串号")

        # 4) 遗忘单槽位。
        applied = await memory.apply(user, "忘掉我的名字", session_id="verify-session-d")
        recalled = await memory.recall_context("我的名字是什么", user)
        print(f"[5] 遗忘: {applied} 召回={recalled!r}")
        if recalled or await memory.list_for(user):
            failures.append("遗忘后仍能召回")
    finally:
        # 验收数据不留在库里，避免污染后续检索。
        await memory.forget_all(user)
        await memory.forget_all(other)
        await client.close()

    if failures:
        print()
        for item in failures:
            print(f"    FAIL: {item}")
        print(f"结论: 长期记忆生命周期=FAIL（{len(failures)} 项）")
        return 1
    print("结论: 长期记忆生命周期=PASS")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        # psycopg AsyncConnectionPool 不支持 Windows 默认 ProactorEventLoop。
        sys.exit(asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop))
    sys.exit(asyncio.run(main()))
