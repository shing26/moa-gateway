from __future__ import annotations

import json

import pytest

from app.audit.wal import AsyncWal, LogConfig
from app.middleware.request_logger import log_request


class FakeRequest:
    method = "POST"
    url = "http://test/webhook/feishu"


class TestLogRequestPolicyFields:
    @pytest.mark.asyncio
    async def test_log_request_with_policy_and_hitl_fields(self, monkeypatch, tmp_path):
        wal = AsyncWal(_config=LogConfig(directory=str(tmp_path), retention_days=90))
        monkeypatch.setattr("app.middleware.request_logger._wal", wal)

        await log_request(
            FakeRequest(), 200, 12.3, "s1", "general", "search", "allow",
            "input", "output",
            policy_hits=("pol-1", "pol-2"),
            hitl_decision="approved",
            hitl_duration_ms=500.0,
        )
        entries = await wal.replay_all()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.policy_hits == ("pol-1", "pol-2")
        assert entry.violation == "pol-1"
        assert entry.hitl_decision == "approved"
        assert entry.hitl_duration_ms == 500.0
        assert entry.extra["policy_hits"] == ("pol-1", "pol-2")
        assert entry.extra["hitl_decision"] == "approved"
        assert entry.extra["hitl_duration_ms"] == 500.0

    @pytest.mark.asyncio
    async def test_log_request_violation_is_first_policy_hit(self, monkeypatch, tmp_path):
        wal = AsyncWal(_config=LogConfig(directory=str(tmp_path), retention_days=90))
        monkeypatch.setattr("app.middleware.request_logger._wal", wal)

        await log_request(
            FakeRequest(), 200, 5.0, "s1", "general", "analyst", "allow",
            "in", "out",
            policy_hits=("pol-a", "pol-b", "pol-c"),
        )
        entry = (await wal.replay_all())[0]
        assert entry.violation == "pol-a"

    @pytest.mark.asyncio
    async def test_log_request_without_new_args_matches_original(self, monkeypatch, tmp_path):
        wal = AsyncWal(_config=LogConfig(directory=str(tmp_path), retention_days=90))
        monkeypatch.setattr("app.middleware.request_logger._wal", wal)

        await log_request(
            FakeRequest(), 200, 12.3, "s1", "general", "search", "allow",
            "用户输入内容", "模型输出内容",
        )
        entry = (await wal.replay_all())[0]
        assert entry.policy_hits == ()
        assert entry.violation == ""
        assert entry.hitl_decision == ""
        assert entry.hitl_duration_ms == 0.0
        assert entry.extra["policy_hits"] == ()
        assert entry.extra["hitl_decision"] == ""
        assert entry.extra["hitl_duration_ms"] == 0.0
        assert entry.extra["input_preview"] == "用户输入内容"
        assert entry.extra["output_preview"] == "模型输出内容"
        assert entry.agent_output == "模型输出内容"

    @pytest.mark.asyncio
    async def test_log_request_disk_format_stays_compatible(self, monkeypatch, tmp_path):
        wal = AsyncWal(_config=LogConfig(directory=str(tmp_path), retention_days=90))
        monkeypatch.setattr("app.middleware.request_logger._wal", wal)

        await log_request(
            FakeRequest(), 200, 12.3, "s1", "general", "search", "denied",
            "input", "output",
            policy_hits=("pol-1",),
            hitl_decision="rejected",
            hitl_duration_ms=250.0,
        )
        files = list(tmp_path.glob("audit-*.jsonl"))
        assert files
        line = files[0].read_text(encoding="utf-8")
        data = json.loads(line)
        assert data["status"] == 200
        assert data["duration_ms"] == 12.3
        assert data["guard_action"] == "denied"
        assert data["input_preview"] == "input"
        assert data["output_preview"] == "output"