from __future__ import annotations
import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger("moa.memory")


class ConversationMemory:
    """In-memory conversation history per session.

    Stores last N turns of user/assistant messages.
    Redis-backed version can replace this later.
    """

    def __init__(self, max_turns: int = 10) -> None:
        self._max = max_turns
        self._store: dict[str, list[dict[str, str]]] = defaultdict(list)

    def add(self, session_id: str, user_msg: str, assistant_msg: str) -> None:
        history = self._store[session_id]
        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": assistant_msg})
        # Trim to max_turns (pairs of messages)
        while len(history) > self._max * 2:
            history.pop(0)
            history.pop(0)

    def get_history(self, session_id: str, limit: int = 10) -> list[dict[str, str]]:
        return self._store.get(session_id, [])[-(limit * 2):]

    def clear(self, session_id: str) -> None:
        self._store.pop(session_id, None)
