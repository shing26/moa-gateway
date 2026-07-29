from __future__ import annotations
import logging

logger = logging.getLogger("moa.command_mode")

MODES = {
    "default": {"label": "默认", "intent": None},
    "coder": {"label": "编程", "intent": "coding"},
    "coding": {"label": "编程", "intent": "coding"},
    "translate": {"label": "翻译", "intent": "translate"},
    "translation": {"label": "翻译", "intent": "translate"},
    "search": {"label": "搜索", "intent": "search"},
    "analyze": {"label": "分析", "intent": "analyze"},
    "analysis": {"label": "分析", "intent": "analyze"},
    "general": {"label": "通用", "intent": None},
}

COMMANDS = {
    "/coding": "coder",
    "/编程": "coder",
    "/编程模式": "coder",
    "/translate": "translate",
    "/翻译": "translate",
    "/翻译模式": "translate",
    "/search": "search",
    "/搜索": "search",
    "/搜索模式": "search",
    "/analyze": "analyze",
    "/分析": "analyze",
    "/分析模式": "analyze",
    "/default": "default",
    "/默认": "default",
    "/通用": "general",
    "/help": None,
    "/帮助": None,
}


class CommandMode:
    def __init__(self):
        self._store: dict[str, str] = {}

    def set(self, session_id: str, mode: str) -> None:
        self._store[session_id] = mode
        logger.info("command mode set session=%s mode=%s", session_id, mode)

    def get(self, session_id: str) -> str | None:
        return self._store.get(session_id)

    def clear(self, session_id: str) -> None:
        self._store.pop(session_id, None)


def parse_command(text: str) -> tuple[str, str] | None:
    text = text.strip()
    if not text.startswith("/"):
        return None
    cmd = text.split()[0].lower()
    mode_key = COMMANDS.get(cmd)
    if mode_key is None:
        if cmd in ("/help", "/帮助"):
            return ("help", "")
        return None
    mode = MODES[mode_key]
    return (mode_key, mode["label"])
