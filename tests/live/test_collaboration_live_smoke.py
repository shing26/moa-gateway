"""真实本地模型的多 Agent 活体 smoke。

默认跳过。显式运行：

    $env:COLLAB_LIVE_SMOKE="1"
    .\\.venv\\Scripts\\python.exe -m pytest tests/live/test_collaboration_live_smoke.py -q -s

该用例不使用当前可能失效的远端端点，而要求本机 Ollama 已拉取
``qwen2.5:3b``。它验证 Supervisor 规划、至少一个专家工具调用、Critic
评审、DAG 收敛和治理交付都在同一条真实链路上发生。
"""

from __future__ import annotations

import json
import os
import sys
import types

import pytest

from app.agents.provider import ChatResult, LLMClient, LLMConfig
from app.agents.stubs import CoderAgent, GeneralAgent
from app.orchestration.collaboration import CollabOrchestrator, EXPERT_KEYS

pytest.importorskip("langgraph", reason="langgraph 是协作图的运行前置")


class RecordingLLMClient:
    """包住真实 client，保留每次调用的 token/延迟快照。"""

    def __init__(self, client: LLMClient) -> None:
        self.client = client
        self.calls: list[dict] = []

    @property
    def last_metrics(self) -> dict:
        return dict(getattr(self.client, "last_metrics", {}) or {})

    def _snapshot(self) -> None:
        self.calls.append(self.last_metrics)

    async def chat(self, messages, **kwargs) -> str:
        result = await self.client.chat(messages, **kwargs)
        self._snapshot()
        return result

    async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResult:
        result = await self.client.chat_with_tools(messages, tools, **kwargs)
        self._snapshot()
        return result


def _cfg() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        collab_llm="litellm",
        collab_max_rounds=2,
        collab_max_subtasks=2,
        collab_workspace_root="",
        hitl_enabled=False,
        default_role="operator",
    )


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("COLLAB_LIVE_SMOKE") != "1",
    reason="设置 COLLAB_LIVE_SMOKE=1 才运行真实模型 smoke",
)
async def test_local_ollama_collaboration_smoke(tmp_path) -> None:
    from app.orchestration.collaboration import LiteLLMCollaborationLLM

    (tmp_path / "sample.py").write_text("SMOKE_VALUE = 'smoke-ok'\n", encoding="utf-8")
    raw_client = LLMClient(
        LLMConfig(
            api_key="ollama",
            base_url="http://localhost:11434/v1",
            model="qwen2.5:3b",
            provider="local",
            temperature=0.0,
            timeout=120.0,
            max_tokens=1024,
        )
    )
    client = RecordingLLMClient(raw_client)
    orchestrator = CollabOrchestrator(
        collaboration_llm=LiteLLMCollaborationLLM(client=client),
        experts={
            "coder": CoderAgent(llm=client),
            "general": GeneralAgent(llm=client),
            "review": GeneralAgent(llm=client),
        },
        settings_obj=_cfg(),
        workspace_root=tmp_path,
    )

    result = await orchestrator.run(
        task=(
            "使用 coder 专家读取 sample.py，并报告文件中的准确内容。"
            "只做这一件事，不要生成其他子任务。"
        ),
        trace_id="collab-live-smoke",
        session_id="collab-live-smoke",
        workspace_path=str(tmp_path),
        verify_command=[sys.executable, "-c", "print('1 passed')"],
    )

    rendered_outputs = "\n".join(result.per_agent_outputs.values())
    assert result.plan
    assert all(item.agent in EXPERT_KEYS for item in result.plan)
    assert result.status in ("ok", "approved")
    assert "smoke-ok" in rendered_outputs
    assert result.tool_calls >= 1
    assert result.rounds >= 1
    assert sum(1 for item in client.calls if item.get("prompt_tokens")) >= 2
    assert raw_client.usage_totals["prompt_hashes"]

    metrics = {
        "model": raw_client.config.model,
        "base_url": raw_client.config.base_url,
        "status": result.status,
        "rounds": result.rounds,
        "tool_calls": result.tool_calls,
        "cost_usd": result.cost_usd,
        "llm_latency_ms": result.llm_latency_ms,
        "prompt_tokens": sum(int(item.get("prompt_tokens", 0) or 0) for item in client.calls),
        "completion_tokens": sum(
            int(item.get("completion_tokens", 0) or 0) for item in client.calls
        ),
        "calls": len(client.calls),
        "prompt_hashes": raw_client.usage_totals["prompt_hashes"],
        "plan": [item.to_dict() for item in result.plan],
        "node_path": list(result.node_path),
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
