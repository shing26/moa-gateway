from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from typing import Any

from app.agents.tools import ToolRegistry
from app.agents.workspace_tools import build_toolset
from apps.code_review_pipeline.agents.coverage_agent import _run_tool_loop
from apps.code_review_pipeline.routing.llm_factory import build_code_review_llm

CRITIC_DECISIONS = frozenset({"keep", "revise", "drop"})

logger = logging.getLogger("moa.code_review.review_critic")


class _CriticMetricsLLM:
    """Accumulate all Critic tool-loop calls instead of exposing only the last one."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.last_metrics: dict[str, Any] = {}
        self._usage: dict[str, Any] = {
            "calls": 0,
            "cost_usd": 0.0,
            "llm_latency_ms": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "models_used": [],
        }

    @property
    def metrics(self) -> dict[str, Any]:
        return {
            **self._usage,
            "models_used": list(self._usage["models_used"]),
        }

    def _record(self) -> None:
        metrics = dict(getattr(self._inner, "last_metrics", {}) or {})
        self.last_metrics = metrics
        if not metrics:
            return
        self._usage["calls"] += 1
        for key in ("cost_usd", "llm_latency_ms"):
            self._usage[key] += float(metrics.get(key, 0.0) or 0.0)
        for key in ("prompt_tokens", "completion_tokens"):
            self._usage[key] += int(metrics.get(key, 0) or 0)
        model = str(metrics.get("model_used") or "")
        if model and model not in self._usage["models_used"]:
            self._usage["models_used"].append(model)

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        result = await self._inner.chat(messages, **kwargs)
        self._record()
        return result

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        **kwargs: Any,
    ) -> Any:
        result = await self._inner.chat_with_tools(messages, tools, **kwargs)
        self._record()
        return result

SYSTEM_PROMPT = """你是代码审查的 Review Critic。你的工作不是重新做一遍完整审查，
而是逐条质证候选 finding，输出严格 JSON：

{
  "verdicts": [
    {
      "finding_uid": "候选 finding 的 uid",
      "decision": "keep|revise|drop",
      "reason": "为什么",
      "instructions": "仅 revise 时填写，必须能让 report worker 直接执行"
    }
  ]
}

规则：
1. 必须对每条候选 finding 给且只给一个 verdict，不得遗漏或新增 uid。
2. keep：diff、工具输出或 RAG 证据足以支持该 finding。
3. revise：问题成立，但描述、严重度、位置或建议需要修正；instructions 必填。
4. drop：问题不成立、位置不在本次 diff、证据不足或只是风格噪声。
5. 可以使用 read_file 和 run_tests；工作区结果优先于推测。
6. 不得把“没有发现证据”解释成“代码没有问题”；没有证据只能 drop 当前 finding。
只输出 JSON，不要其他内容。"""


@dataclass(frozen=True)
class ReviewCriticVerdict:
    finding_uid: str
    decision: str
    reason: str
    instructions: str = ""

    def __post_init__(self) -> None:
        if self.decision not in CRITIC_DECISIONS:
            raise ValueError(f"unsupported critic decision: {self.decision}")
        if not self.reason.strip():
            raise ValueError("critic reason is required")
        if self.decision == "revise" and not self.instructions.strip():
            raise ValueError("critic instructions are required for revise")


@dataclass(frozen=True)
class ReviewCriticResult:
    verdicts: tuple[ReviewCriticVerdict, ...]
    metrics: dict[str, Any] | None = None
    tool_outputs: dict[str, str] | None = None


class ReviewCriticAgent:
    def __init__(self, llm: Any | None = None) -> None:
        self._llm = llm

    async def review(
        self,
        *,
        trace_id: str,
        pr_title: str,
        diff: str,
        findings: tuple[Any, ...],
        workspace_path: Path | str | None = None,
        verify_command: Sequence[str] = (),
    ) -> ReviewCriticResult:
        if not findings:
            return ReviewCriticResult(verdicts=(), metrics={}, tool_outputs={})
        llm = _CriticMetricsLLM(self._llm or build_code_review_llm())
        candidates = [
            {
                "finding_uid": str(item.finding_uid),
                "severity": str(item.severity),
                "category": str(item.category),
                "file": str(item.file),
                "line": int(item.line),
                "title": str(item.title),
                "description": str(item.description),
                "suggestion": str(item.suggestion),
                "confidence": float(item.confidence),
                "evidence": list(item.evidence),
                "source_agents": list(item.source_agents),
            }
            for item in findings
        ]
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "trace_id": trace_id,
                        "pr_title": pr_title,
                        "diff": diff,
                        "candidate_findings": candidates,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        workspace = Path(workspace_path) if workspace_path else None
        if workspace is None:
            tools = ToolRegistry()
        elif not tuple(verify_command):
            tools = build_toolset(
                workspace=workspace,
                verify_command=(),
                names=("read_file",),
            )
        else:
            tools = build_toolset(
                workspace=workspace,
                verify_command=tuple(str(item) for item in verify_command),
                names=("read_file", "run_tests"),
            )
        raw, _, _, tool_outputs = await _run_tool_loop(
            llm,
            messages,
            tools,
            workspace=workspace or Path.cwd(),
        )
        parsed = parse_review_critic_result(raw, tuple(findings))
        return ReviewCriticResult(
            verdicts=parsed.verdicts,
            metrics=llm.metrics,
            tool_outputs=tool_outputs,
        )


def parse_review_critic_result(
    raw: str,
    candidates: tuple[Any, ...],
) -> ReviewCriticResult:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid critic JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("verdicts"), list):
        raise TypeError("critic output must contain a verdicts list")

    expected = {str(item.finding_uid) for item in candidates}
    seen: set[str] = set()
    verdicts: list[ReviewCriticVerdict] = []
    for item in payload["verdicts"]:
        if not isinstance(item, dict):
            raise TypeError("critic verdict must be an object")
        finding_uid = str(item.get("finding_uid") or "").strip()
        if finding_uid not in expected:
            raise ValueError(f"critic referenced unknown finding: {finding_uid}")
        if finding_uid in seen:
            raise ValueError(f"duplicate critic verdict: {finding_uid}")
        seen.add(finding_uid)
        verdicts.append(
            ReviewCriticVerdict(
                finding_uid=finding_uid,
                decision=str(item.get("decision") or "").strip(),
                reason=str(item.get("reason") or "").strip(),
                instructions=str(item.get("instructions") or "").strip(),
            )
        )
    missing = sorted(expected - seen)
    if missing:
        raise ValueError(f"critic omitted findings: {', '.join(missing)}")
    return ReviewCriticResult(verdicts=tuple(verdicts))
