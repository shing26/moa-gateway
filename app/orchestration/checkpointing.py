"""Persistent LangGraph checkpoint construction for collaboration HITL."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


@dataclass
class OwnedCheckpointer:
    saver: Any
    connection: aiosqlite.Connection

    async def aclose(self) -> None:
        await self.connection.close()


def open_sqlite_checkpointer(path: Path | str) -> OwnedCheckpointer:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = aiosqlite.connect(target, timeout=5.0)
    return OwnedCheckpointer(
        saver=AsyncSqliteSaver(connection),
        connection=connection,
    )


__all__ = ["OwnedCheckpointer", "open_sqlite_checkpointer"]
