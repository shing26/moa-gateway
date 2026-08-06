from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.audit.es_writer import EsWriter, build_es_writer
from app.audit.models import AuditEntry
from app.audit.wal import AsyncWal, LogConfig
from app.middleware.request_logger import log_request


class FakeRequest:
    method = "POST"
    url = "http://test/webhook/feishu"


class TestBuildEsWriter:
    def test_hosts_empty_returns_none(self):
        settings = SimpleNamespace(es_hosts=[], es_index_prefix="moa-audit")
        assert build_es_writer(settings) is None

    def test_hosts_none_returns_none(self):
        settings = SimpleNamespace(es_hosts=None, es_index_prefix="moa-audit")
        assert build_es_writer(settings) is None

    def test_hosts_present_returns_writer_with_config(self):
        settings = SimpleNamespace(
            es_hosts=["http://localhost:9200", "http://es2:9200"],
            es_index_prefix="custom-audit",
        )
        writer = build_es_writer(settings)
        assert isinstance(writer, EsWriter)
        assert writer.config.hosts == ["http://localhost:9200", "http://es2:9200"]
        assert writer.config.index_prefix == "custom-audit"
        assert writer.config.bulk_size == 100

    @pytest.mark.asyncio
    async def test_writer_closes_cleanly_without_es(self):
        settings = SimpleNamespace(es_hosts=["http://localhost:9200"], es_index_prefix="moa-audit")
        writer = build_es_writer(settings)
        assert isinstance(writer, EsWriter)
        await writer.aclose()


class TestDualWrite:
    @pytest.mark.asyncio
    async def test_log_request_writes_wal_and_es(self, monkeypatch, tmp_path):
        wal = AsyncWal(_config=LogConfig(directory=str(tmp_path), retention_days=90))
        monkeypatch.setattr("app.middleware.request_logger._wal", wal)
        es_calls = []

        class FakeEs:
            async def write(self, entry):
                es_calls.append(entry)

        monkeypatch.setattr("app.deps.es_writer", FakeEs())

        await log_request(
            FakeRequest(), 200, 12.3, "s1", "general", "search", "allow",
            "in", "out",
        )
        entries = await wal.replay_all()
        assert len(entries) == 1
        assert len(es_calls) == 1
        assert isinstance(es_calls[0], AuditEntry)
        assert es_calls[0].trace_id == entries[0].trace_id
        assert es_calls[0].session_id == "s1"

    @pytest.mark.asyncio
    async def test_es_write_exception_does_not_break_main_flow(self, monkeypatch, tmp_path):
        wal = AsyncWal(_config=LogConfig(directory=str(tmp_path), retention_days=90))
        monkeypatch.setattr("app.middleware.request_logger._wal", wal)

        class RaisingEs:
            async def write(self, entry):
                raise RuntimeError("es down")

        monkeypatch.setattr("app.deps.es_writer", RaisingEs())

        await log_request(
            FakeRequest(), 200, 12.3, "s1", "general", "search", "allow",
            "in", "out",
        )
        entries = await wal.replay_all()
        assert len(entries) == 1
        assert entries[0].agent_output == "out"

    @pytest.mark.asyncio
    async def test_no_es_writer_single_wal(self, monkeypatch, tmp_path):
        wal = AsyncWal(_config=LogConfig(directory=str(tmp_path), retention_days=90))
        monkeypatch.setattr("app.middleware.request_logger._wal", wal)
        monkeypatch.setattr("app.deps.es_writer", None)

        await log_request(
            FakeRequest(), 200, 12.3, "s1", "general", "search", "allow",
            "in", "out",
        )
        entries = await wal.replay_all()
        assert len(entries) == 1
        assert entries[0].agent_output == "out"
