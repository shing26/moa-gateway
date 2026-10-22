from __future__ import annotations

import types
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.orchestration.collaboration import (
    CollabOrchestrator,
    ScriptedExpert,
)

pytest.importorskip(
    "langgraph.checkpoint.sqlite",
    reason="langgraph-checkpoint-sqlite 是持久化恢复所需的可选 extra",
)

ROOT = Path(__file__).resolve().parents[2]


class PlanLLM:
    async def plan(self, *, task: str):
        return [
            {
                "index": 0,
                "instruction": "写点啥",
                "agent": "general",
                "depends_on": [],
            }
        ]

    async def critique(self, *, task: str, plan, outputs):
        return {"decision": "approve", "reasons": [], "targets": []}


def _settings(checkpoint_path: str):
    return types.SimpleNamespace(
        collab_llm="mock",
        collab_max_rounds=2,
        collab_max_subtasks=4,
        hitl_enabled=True,
        default_role="operator",
        collab_checkpoint_path=checkpoint_path,
    )


@pytest.mark.asyncio
async def test_sqlite_checkpoint_preserves_hitl_across_orchestrator_restart(
    tmp_path,
) -> None:
    checkpoint_path = str(tmp_path / "collab-checkpoints.sqlite3")
    experts = {
        "general": ScriptedExpert(reply="TODO: 需要人工复核"),
    }
    first = CollabOrchestrator(
        collaboration_llm=PlanLLM(),
        experts=experts,
        settings_obj=_settings(checkpoint_path),
    )
    suspended = await first.run(task="写点啥", trace_id="t-restart")
    assert suspended.status == "pending_review"
    await first.aclose()

    second = CollabOrchestrator(
        collaboration_llm=PlanLLM(),
        experts=experts,
        settings_obj=_settings(checkpoint_path),
    )
    try:
        payload = await second.apending_payload("t-restart")
        assert payload is not None
        assert payload["trace_id"] == "t-restart"

        approved = await second.resume("t-restart", "approve")
        assert approved.status == "approved"
        assert "TODO" in approved.final_text
    finally:
        await second.aclose()


_SUBPROCESS_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import json
    import sys
    import types

    from app.orchestration.collaboration import CollabOrchestrator, ScriptedExpert


    class PlanLLM:
        async def plan(self, *, task):
            return [
                {
                    "index": 0,
                    "instruction": "写点啥",
                    "agent": "general",
                    "depends_on": [],
                }
            ]

        async def critique(self, *, task, plan, outputs):
            return {"decision": "approve", "reasons": [], "targets": []}


    def build(path):
        return CollabOrchestrator(
            collaboration_llm=PlanLLM(),
            experts={"general": ScriptedExpert(reply="TODO: need review")},
            settings_obj=types.SimpleNamespace(
                collab_llm="mock",
                collab_max_rounds=2,
                collab_max_subtasks=4,
                hitl_enabled=True,
                default_role="operator",
                collab_checkpoint_path=path,
            ),
        )


    async def main():
        path, mode, trace_id = sys.argv[1:]
        orchestrator = build(path)
        try:
            if mode == "suspend":
                result = await orchestrator.run(task="write", trace_id=trace_id)
                print(json.dumps({"status": result.status}))
            else:
                pending = await orchestrator.apending_payload(trace_id)
                result = await orchestrator.resume(trace_id, "approve")
                print(json.dumps({
                    "pending": pending is not None,
                    "status": result.status,
                }))
        finally:
            await orchestrator.aclose()


    asyncio.run(main())
    """
)


def test_sqlite_checkpoint_survives_separate_processes(tmp_path) -> None:
    checkpoint_path = str(tmp_path / "cross-process.sqlite3")
    trace_id = "t-cross-process"

    suspended = subprocess.run(
        [
            sys.executable,
            "-c",
            _SUBPROCESS_SCRIPT,
            checkpoint_path,
            "suspend",
            trace_id,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(suspended.stdout)["status"] == "pending_review"

    resumed = subprocess.run(
        [
            sys.executable,
            "-c",
            _SUBPROCESS_SCRIPT,
            checkpoint_path,
            "resume",
            trace_id,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(resumed.stdout) == {
        "pending": True,
        "status": "approved",
    }
