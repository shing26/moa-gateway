from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.models.events import MoAEvent
from app.pipeline import PipelineResult
from evals.run_evals import (
    load_dataset,
    run_all,
    run_e2e_offline,
    run_e2e_eval,
    run_guard_eval,
    run_intent_eval,
    write_report,
)


@pytest.mark.asyncio
async def test_intent_eval_metrics() -> None:
    cases = [
        {"id": "i1", "input": "你好", "expected_intent": "greeting"},
        {"id": "i2", "input": "搜索文档", "expected_intent": "search"},
        {"id": "i3", "input": "讲个笑话", "expected_intent": "assistant"},
    ]

    report = await run_intent_eval(cases)

    assert report["total"] == 3
    assert report["correct"] == 3
    assert report["accuracy"] == 1.0
    assert report["confusion"]["greeting"]["greeting"] == 1


@pytest.mark.asyncio
async def test_guard_eval_metrics() -> None:
    cases = [
        {"id": "g1", "input": "服务器 192.168.1.1", "expected_action": "deny"},
        {"id": "g2", "input": "报价 5 万元", "expected_action": "review"},
        {"id": "g3", "input": "今天天气不错", "expected_action": "allow"},
    ]

    report = await run_guard_eval(cases)

    assert report["total"] == 3
    assert report["accuracy"] == 1.0
    assert report["deny_recall"] == 1.0
    assert report["deny_precision"] == 1.0
    assert report["false_positive"] == 0
    assert report["review_recall"] == 1.0
    assert report["review_precision"] == 1.0
    assert report["mislabeled"] == 0


class RecordingOfflinePipeline:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, event: MoAEvent, *, channel: str, target: str) -> PipelineResult:
        self.calls += 1
        return PipelineResult(
            trace_id=event.trace_id, state="ROUTED", intent="assistant",
            text="offline fake", status="ok",
        )


@pytest.mark.asyncio
async def test_offline_e2e_exercises_fake_pipeline_and_marks_skipped() -> None:
    cases = [{"id": "e1"}, {"id": "e2"}]
    runner = RecordingOfflinePipeline()

    report = await run_e2e_offline(cases, pipeline=runner)

    assert report["total"] == 2
    assert report["run"] == 0
    assert report["skipped"] == 2
    assert report["offline_smoke"] == 2
    assert runner.calls == 2


@pytest.mark.asyncio
async def test_e2e_eval_aggregates_cost_and_judge_score() -> None:
    cases = [
        {"id": "e1", "input": "hi", "expected": {"status": "ok"}, "judge_criteria": "x"},
        {"id": "e2", "input": "yo", "expected": {"status": "ok"}, "judge_criteria": "x"},
    ]

    class FakePipeline:
        async def run(self, event, *, channel, target):
            await asyncio.sleep(0.001)
            return PipelineResult(
                trace_id=event.trace_id, state="ROUTED", intent="assistant",
                text="fake output", status="ok", cost_usd=0.01,
            )

    async def fake_judge(input_text, output_text, criteria) -> float:
        return 0.8

    # use_store=False：注入测试替身时不该被拖去连真实 pgvector
    report = await run_e2e_eval(
        cases, pipeline=FakePipeline(), judge=fake_judge, use_store=False,
    )

    assert report["run"] == 2
    assert report["skipped"] == 0
    assert report["avg_cost_usd"] == 0.01
    assert report["avg_judge_score"] == 0.8
    assert report["avg_latency_ms"] > 0


@pytest.mark.asyncio
async def test_e2e_eval_store_lifecycle_is_explicit(monkeypatch) -> None:
    """真实存储的启停必须是显式开关，不能从 ``pipeline is None`` 推断。

    回归：早先 ``use_store = pipeline is None``，而 ``--engine fsm|langgraph`` 传入的
    是**真实** runner（fsm_pipeline / LangGraphOrchestrator），于是被判成"注入了假
    pipeline"→ 跳过 ``vector_client.start()`` → pgvector 的 ``_pool`` 为 None →
    search 静默返回空。**那两个引擎跑的其实是没有 RAG 上下文的链路**，而 e2e 照样报
    success 1.0（日志里只有一行 `后端已降级，search 返回空结果（None）`）。
    """
    from app import deps

    lifecycle: list[str] = []

    class RecordingStore:
        async def start(self) -> None:
            lifecycle.append("start")

        async def close(self) -> None:
            lifecycle.append("close")

    monkeypatch.setattr(deps, "vector_client", RecordingStore())

    class FakePipeline:
        async def run(self, event, *, channel, target):
            return PipelineResult(
                trace_id=event.trace_id, state="ROUTED", intent="assistant",
                text="fake", status="ok",
            )

    async def fake_judge(_input: str, _output: str, _criteria: str) -> float:
        return 1.0

    cases = [{"id": "e1", "input": "hi", "expected": {"status": "ok"}}]

    # 默认（= run_all 与 --engine 走的路径）：真实 runner，必须启停真实存储
    await run_e2e_eval(cases, pipeline=FakePipeline(), judge=fake_judge)
    assert lifecycle == ["start", "close"]

    # 注入测试替身的调用方显式 opt out：不连库，也不受本机 DSN 影响
    lifecycle.clear()
    await run_e2e_eval(
        cases, pipeline=FakePipeline(), judge=fake_judge, use_store=False,
    )
    assert lifecycle == []


def test_judge_config_defaults_to_project_llm_config(monkeypatch) -> None:
    """评测器默认复用 ``LLM_*`` 配置，而不是硬编码 OpenAI 的 gpt-4o-mini。

    2026-09-22 实测缺陷：judge 的 base_url 取 ``OPENAI_BASE_URL``（local 形态下指向
    Ollama），model 却是硬编码默认 ``gpt-4o-mini`` —— 两个来源混用，请求打到一个没有
    该模型的端点，报 ``model 'gpt-4o-mini' not found``。这是 e2e 长期只能 ``--offline``
    的原因之一：真跑一次就炸。
    """
    from evals.judge import _judge_config

    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "moa-qwen")

    cfg = _judge_config()
    assert "moa-qwen" in cfg.model
    assert cfg.model != "gpt-4o-mini"
    assert cfg.base_url == "http://localhost:11434/v1"


def test_judge_config_honours_explicit_judge_model(monkeypatch) -> None:
    """显式设了 JUDGE_MODEL 时走 JUDGE_* 全家（评测想用更强模型的口子）。"""
    from evals.judge import _judge_config

    monkeypatch.setenv("JUDGE_MODEL", "gpt-4o")
    monkeypatch.setenv("JUDGE_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("JUDGE_PROVIDER", "openai")

    cfg = _judge_config()
    # openai 属 openai 兼容提供方，_qualify_model 会加 litellm 前缀
    assert cfg.model == "openai/gpt-4o"
    assert cfg.base_url == "https://api.example/v1"


def test_load_dataset_parses_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "intent.jsonl"
    path.write_text(
        '{"id":"a","input":"x","expected_intent":"assistant"}\n'
        '{"id":"b","input":"y","expected_intent":"search"}\n',
        encoding="utf-8",
    )

    cases = load_dataset(path)

    assert len(cases) == 2
    assert cases[0]["expected_intent"] == "assistant"
    assert cases[1]["id"] == "b"


def test_write_report(tmp_path: Path) -> None:
    report_path = tmp_path / "reports" / "latest.json"
    write_report({"summary": "ok", "intent": {"accuracy": 1.0}}, report_path)

    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["summary"] == "ok"
    assert data["intent"]["accuracy"] == 1.0


@pytest.mark.asyncio
async def test_run_all_offline(tmp_path: Path) -> None:
    (tmp_path / "intent.jsonl").write_text(
        '{"id":"i1","input":"你好","expected_intent":"greeting"}\n',
        encoding="utf-8",
    )
    (tmp_path / "guard_redteam.jsonl").write_text(
        '{"id":"g1","input":"服务器 192.168.1.1","expected_action":"deny"}\n',
        encoding="utf-8",
    )
    (tmp_path / "e2e.jsonl").write_text(
        '{"id":"e1","input":"hi","expected":{"status":"ok"},"judge_criteria":"x"}\n',
        encoding="utf-8",
    )
    (tmp_path / "tool_selection.jsonl").write_text(
        '{"id":"t1","input":"现在几点","expected_tool":"current_time"}\n',
        encoding="utf-8",
    )

    report = await run_all(offline=True, datasets_dir=tmp_path)

    assert report["intent"]["accuracy"] == 1.0
    assert report["guard"]["deny_recall"] == 1.0
    assert report["e2e"]["skipped"] == 1
    assert report["e2e"]["offline_smoke"] == 1
    assert report["tool_selection"]["accuracy"] == 1.0
    assert report["agent_metrics"]["tool_selection_accuracy"] == 1.0
