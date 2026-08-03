from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("moa.obsidian_sync")


class ObsidianVaultSync:
    """Watches a local Obsidian vault and mirrors markdown notes into the knowledge base.

    File identity is the note's relative path inside the watched root, so edits
    overwrite the same knowledge doc and deletes remove it.
    """

    def __init__(
        self,
        vault_path: str = "",
        subfolder: str = "",
        knowledge_base: Any = None,
        interval: float = 2.0,
    ) -> None:
        self.vault_path = vault_path
        self.subfolder = subfolder
        self._kb = knowledge_base
        self._interval = interval
        self._state: dict[str, tuple[int, int]] = {}
        self._task: asyncio.Task | None = None
        self.last_sync = ""
        self.last_error = ""
        self.synced_docs = 0

    @property
    def root(self) -> pathlib.Path | None:
        if not self.vault_path:
            return None
        root = pathlib.Path(self.vault_path)
        if self.subfolder:
            root = root / self.subfolder
        return root if root.is_dir() else None

    @property
    def enabled(self) -> bool:
        return self.root is not None and self._kb is not None

    @classmethod
    def from_env(cls, knowledge_base: Any) -> "ObsidianVaultSync":
        import os

        return cls(
            vault_path=os.environ.get("OBSIDIAN_VAULT_PATH", ""),
            subfolder=os.environ.get("OBSIDIAN_SYNC_FOLDER", ""),
            knowledge_base=knowledge_base,
        )

    def _rel_key(self, path: pathlib.Path) -> str:
        return path.relative_to(self.root).as_posix()

    @staticmethod
    def _doc_id(rel: str) -> str:
        return "obs:" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]

    def _scan(self) -> dict[str, tuple[int, int]]:
        root = self.root
        if root is None:
            return {}
        snapshot: dict[str, tuple[int, int]] = {}
        for path in root.rglob("*.md"):
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[self._rel_key(path)] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    async def sync_once(self) -> int:
        if not self.enabled:
            return 0
        current = self._scan()
        changed = 0
        for rel, signature in current.items():
            if self._state.get(rel) == signature:
                continue
            path = self.root / pathlib.Path(rel)
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                self.last_error = f"{rel}: {exc}"
                logger.warning("obsidian read failed: %s", self.last_error)
                continue
            await self._kb.add_document(title=rel, content=content, doc_id=self._doc_id(rel))
            self._state[rel] = signature
            changed += 1
        for rel in list(self._state):
            if rel not in current:
                await self._kb.delete_doc(self._doc_id(rel))
                del self._state[rel]
                changed += 1
        self.last_sync = datetime.now(timezone.utc).isoformat()
        self.synced_docs = len(self._state)
        if changed:
            logger.info("obsidian sync changed=%d docs=%d", changed, self.synced_docs)
        return changed

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        await self.sync_once()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.sync_once()
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("obsidian sync loop error")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def status(self) -> dict[str, Any]:
        root = self.root
        return {
            "enabled": self.enabled,
            "root": str(root) if root else "",
            "subfolder": self.subfolder,
            "docs": self.synced_docs,
            "last_sync": self.last_sync,
            "last_error": self.last_error,
        }
