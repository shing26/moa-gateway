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
        assert entry.extra["llm_model"] == ""
        assert entry.extra["cost_usd"] == 0.0
        assert entry.extra["llm_latency_ms"] == 0.0
        assert entry.extra["fallback_used"] == ""

    @pytest.mark.asyncio
    async def test_log_request_with_llm_metrics_fields(self, monkeypatch, tmp_path):
        wal = AsyncWal(_config=LogConfig(directory=str(tmp_path), retention_days=90))
        monkeypatch.setattr("app.middleware.request_logger._wal", wal)

        await log_request(
            FakeRequest(), 200, 12.3, "s1", "coder", "coding", "allow",
            "input", "output",
            llm_model="gpt-4o-mini",
            cost_usd=0.0123,
            llm_latency_ms=456.7,
            fallback_used="gpt-3.5-turbo",
        )
        entry = (await wal.replay_all())[0]
        assert entry.extra["llm_model"] == "gpt-4o-mini"
        assert entry.extra["cost_usd"] == 0.0123
        assert entry.extra["llm_latency_ms"] == 456.7
        assert entry.extra["fallback_used"] == "gpt-3.5-turbo"

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


class TestOptionalExtraFields:
    """只在真的发生时才写进 extra —— 既有审计条目的字段形状不能变。

    retry_count / retry_reason / hitl_kind / tool_calls / tool_errors 都是这样：
    缺席本身有含义（"没重试""没有工具活动"），无脑写 0 会把这个区分抹掉。
    """

    async def _one_entry(self, monkeypatch, tmp_path, **kwargs):
        wal = AsyncWal(_config=LogConfig(directory=str(tmp_path), retention_days=90))
        monkeypatch.setattr("app.middleware.request_logger._wal", wal)
        await log_request(
            FakeRequest(), 200, 1.0, "s1", "task", "task", "allow",
            "in", "out", **kwargs,
        )
        return (await wal.replay_all())[0]

    @pytest.mark.asyncio
    async def test_absent_when_they_did_not_happen(self, monkeypatch, tmp_path):
        entry = await self._one_entry(monkeypatch, tmp_path)
        for key in (
            "retry_count", "retry_reason", "hitl_kind", "tool_calls", "tool_errors",
            "route_fallback",
        ):
            assert key not in entry.extra, f"{key} 不该在没发生时出现"

    @pytest.mark.asyncio
    async def test_route_fallback_records_degradation(self, monkeypatch, tmp_path):
        """路由层级要落审计——`none` 就是"微模型/路由 LLM 没给出判定，intent 是默认值"。

        此前审计只记 intent 不记层级，于是"路由一直在超时降级"这件事在数据里
        完全看不见。
        """
        entry = await self._one_entry(monkeypatch, tmp_path, route_fallback="none")
        assert entry.extra["route_fallback"] == "none"

        ok = await self._one_entry(monkeypatch, tmp_path, route_fallback="regex")
        assert ok.extra["route_fallback"] == "regex"

    @pytest.mark.asyncio
    async def test_present_when_they_happened(self, monkeypatch, tmp_path):
        entry = await self._one_entry(
            monkeypatch, tmp_path,
            retry_count=1, retry_reason="RuntimeError: boom",
            hitl_kind="failure_escalation", tool_calls=3, tool_errors=3,
        )
        assert entry.extra["retry_count"] == 1
        assert entry.extra["retry_reason"] == "RuntimeError: boom"
        assert entry.extra["hitl_kind"] == "failure_escalation"
        # 3 次调用 3 次失败 = "所有工具都失败但任务仍返回了结果"
        assert entry.extra["tool_calls"] == 3
        assert entry.extra["tool_errors"] == 3

    @pytest.mark.asyncio
    async def test_tool_errors_alone_are_recorded(self, monkeypatch, tmp_path):
        """只有失败、没有成功调用时同样留痕（tool_calls 可以是 0）。"""
        entry = await self._one_entry(monkeypatch, tmp_path, tool_errors=2)
        assert entry.extra["tool_errors"] == 2
        assert entry.extra["tool_calls"] == 0
