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
            return await self._fallback_wal(entries)

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
                return await self._fallback_wal(entries)
            logger.info("es bulk wrote %d entries", len(entries))
            return True
        except Exception as exc:
            logger.warning("es write failed, falling back to wal: %s", exc)
            return await self._fallback_wal(entries)

    async def _fallback_wal(self, entries: list[AuditEntry]) -> bool:
        try:
            for entry in entries:
                await self.wal.append(entry)
            return True
        except Exception:
            return False

    def _build_bulk_body(self, entries: list[AuditEntry]) -> bytes:
        lines: list[str] = []
        for entry in entries:
            action = json.dumps({"index": {"_index": self.config.index_prefix}}, ensure_ascii=False)
            # 字段集合与 WAL 同源（AuditEntry.to_audit_dict()）。此前 ES 侧另有一份
            # 手写清单，比 WAL 还少 policy_hits/hitl_decision/status/duration_ms/previews，
            # 两个 sink 的审计数据因此不一致。ES 保留 agent_output 全文以便检索。
            doc = entry.to_audit_dict()
            doc["@timestamp"] = doc["timestamp"]  # ES 约定字段名
            lines.append(action)
            lines.append(json.dumps(doc, ensure_ascii=False))
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


def build_es_writer(settings: Any) -> EsWriter | None:
    hosts = getattr(settings, "es_hosts", None) or []
    if not hosts:
        return None
    config = EsConfig(
        hosts=[str(h) for h in hosts],
        index_prefix=getattr(settings, "es_index_prefix", "moa-audit"),
    )
    return EsWriter(config=config)
