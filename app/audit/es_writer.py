from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.audit.models import AuditEntry
from app.audit.wal import AsyncWal

logger = logging.getLogger("moa.audit.es_writer")


@dataclass
class EsConfig:
    hosts: list[str] = field(default_factory=lambda: ["http://localhost:9200"])
    index_prefix: str = "moa-audit"
    bulk_size: int = 100
    timeout: float = 10.0


class EsWriter:
    """Async bulk writer for Elasticsearch audit logs.

    Falls back to the provided AsyncWal when ES is unreachable.
    """

    def __init__(self, config: EsConfig | None = None, wal: AsyncWal | None = None) -> None:
        self.config = config or EsConfig()
        self.wal = wal or AsyncWal()
        self._buffer: list[AuditEntry] = []
        self._client: httpx.AsyncClient | None = None

    async def write(self, entry: AuditEntry) -> bool:
        self._buffer.append(entry)
        if len(self._buffer) >= self.config.bulk_size:
            return await self.flush()
        return True

    async def flush(self) -> bool:
        if not self._buffer:
            return True
        batch = self._buffer
        self._buffer = []
        return await self._send_batch(batch)

    async def _send_batch(self, entries: list[AuditEntry]) -> bool:
        if not self.config.hosts:
            return self._fallback_wal(entries)

        try:
            body = self._build_bulk_body(entries)
            client = await self._get_client()
            host = self.config.hosts[0]
            url = f"{host.rstrip('/')}/{self.config.index_prefix}/_bulk"
            resp = await client.post(url, content=body, headers={"Content-Type": "application/x-ndjson"})
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                logger.error("es bulk returned errors: %s", data)
                return self._fallback_wal(entries)
            logger.info("es bulk wrote %d entries", len(entries))
            return True
        except Exception as exc:
            logger.warning("es write failed, falling back to wal: %s", exc)
            return self._fallback_wal(entries)

    def _fallback_wal(self, entries: list[AuditEntry]) -> bool:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            for entry in entries:
                loop.run_until_complete(self.wal.append(entry))
            return True
        except Exception:
            return False

    def _build_bulk_body(self, entries: list[AuditEntry]) -> bytes:
        lines: list[str] = []
        for entry in entries:
            action = json.dumps({"index": {"_index": self.config.index_prefix}}, ensure_ascii=False)
            doc = json.dumps({
                "@timestamp": entry.timestamp.isoformat(),
                "trace_id": entry.trace_id,
                "session_id": entry.session_id,
                "agent_name": entry.agent_name,
                "intent": entry.intent,
                "agent_output": entry.agent_output,
                "eval_score": entry.eval_score,
                "eval_issues": list(entry.eval_issues),
                "guard_action": entry.guard_action,
                "guard_reason": entry.guard_reason,
            }, ensure_ascii=False)
            lines.append(action)
            lines.append(doc)
        return ("\n".join(lines) + "\n").encode("utf-8")

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout)
        return self._client

    async def aclose(self) -> None:
        await self.flush()
        if self._client:
            await self._client.aclose()
            self._client = None
