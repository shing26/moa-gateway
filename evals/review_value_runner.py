"""Execute the locked R0/R1/R1+I review-value evaluation matrix.

The runner records model findings and protocol facts only. Human disposition
labels remain empty until a reviewer fills them in; this module never converts a
model prelabel into human evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from app.agents.provider import LLMClient, LLMConfig
from app.models.events import MoAEvent
from apps.code_review_pipeline.agents.code_review_pipeline import CodeReviewPipeline
from apps.code_review_pipeline.agents.review_critic import ReviewCriticAgent
from apps.code_review_pipeline.routing.github_client import GitHubClient
from apps.code_review_pipeline.routing.github_provider import build_github_client
from apps.code_review_pipeline.routing.llm_factory import build_code_review_llm
from apps.code_review_pipeline.schemas.pr_context import PRContext, PRFile
from apps.code_review_pipeline.workspace import GitHubArchiveWorkspaceProvisioner
from evals.review_value import (
    ReviewEvaluationCase,
    ReviewRunRecord,
    controlled_review_input,
    load_controlled_cases,
    load_pr_corpus,
)

EvalRunner = Callable[[str, str, str, str], Awaitable[ReviewRunRecord]]

PRELABEL_SYSTEM_PROMPT = """你是独立的代码审查 finding 预标器。你只做初标，不能冒充人工终审。
读取 finding 和对应 diff，输出严格 JSON：
{
  "disposition": "accepted_actionable|accepted_non_actionable|false_positive|duplicate|already_known",
  "reason_code": "false_positive 或 accepted_non_actionable 时必填",
  "rationale": "一句话依据"
}
只输出 JSON，不要其他内容。"""


class MeteredLLM:
    """Accumulate metrics while delegating to the configured review LLM."""

    def __init__(self, inner: Any, *, token_budget: int = 0) -> None:
        self._inner = inner
        self._token_budget = max(int(token_budget), 0)
        self.last_metrics: dict[str, Any] = {}
        self.usage: dict[str, Any] = {
            "calls": 0,
            "cost_usd": 0.0,
            "latency_ms": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "models_used": [],
        }

    def _record(self) -> None:
        metrics = dict(getattr(self._inner, "last_metrics", {}) or {})
        self.last_metrics = metrics
        if not metrics:
            return
        self.usage["calls"] += 1
        self.usage["cost_usd"] += float(metrics.get("cost_usd", 0.0) or 0.0)
        self.usage["latency_ms"] += float(metrics.get("llm_latency_ms", 0.0) or 0.0)
        self.usage["prompt_tokens"] += int(metrics.get("prompt_tokens", 0) or 0)
        self.usage["completion_tokens"] += int(metrics.get("completion_tokens", 0) or 0)
        model = str(metrics.get("model_used") or "")
        if model and model not in self.usage["models_used"]:
            self.usage["models_used"].append(model)
        used_tokens = int(self.usage["prompt_tokens"]) + int(
            self.usage["completion_tokens"]
        )
        if self._token_budget and used_tokens > self._token_budget:
            raise RuntimeError(
                "evaluation token budget exceeded: "
                f"{used_tokens} > {self._token_budget}"
            )

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

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


def _parse_patch(patch: str) -> tuple[PRFile, ...]:
    files: list[PRFile] = []
    for chunk in re.split(r"(?m)(?=^diff --git )", patch):
        if not chunk.startswith("diff --git "):
            continue
        first = chunk.splitlines()[0]
        match = re.search(r" b/(.+)$", first)
        if not match:
            continue
        path = match.group(1).strip()
        additions = sum(
            line.startswith("+") and not line.startswith("+++")
            for line in chunk.splitlines()
        )
        deletions = sum(
            line.startswith("-") and not line.startswith("---")
            for line in chunk.splitlines()
        )
        status = (
            "added"
            if "new file mode" in chunk
            else "removed"
            if "deleted file mode" in chunk
            else "modified"
        )
        files.append(
            PRFile(
                filename=path,
                status=status,
                additions=additions,
                deletions=deletions,
                changes=additions + deletions,
                patch=chunk,
                content=chunk if path.endswith(".py") else None,
            )
        )
    return tuple(files)


def _controlled_pr(case: ReviewEvaluationCase) -> tuple[PRContext, MoAEvent]:
    payload = controlled_review_input(case)
    files = _parse_patch(str(payload["diff"]))
    pr = PRContext(
        repo=f"controlled/{case.source.lower()}",
        pr_number=0,
        head_sha=str(payload["head_sha"]),
        base_sha=str(payload["base_commit"]),
        title=f"Controlled review case {case.case_id}",
        author="evaluation",
        html_url="",
        diff_url="",
        changed_files=list(files),
    )
    event = MoAEvent(
        trace_id=f"eval_{case.case_id}",
        event=None,
        session_id=pr.repo,
        text="",
        context={},
    )
    return pr, event


def _github_event(row: dict[str, Any], arm: str) -> MoAEvent:
    repo = str(row["repo"])
    number = int(row["number"])
    return MoAEvent(
        trace_id=f"eval_{row['pr_id']}_{arm.lower()}",
        event=None,
        session_id=repo,
        text="",
        context={
            "action": "synchronize",
            "repository": {"full_name": repo},
            "pull_request": {
                "number": number,
                "head": {"sha": str(row["head_sha"])},
                "base": {"sha": str(row["base_sha"])},
                "title": str(row.get("title") or ""),
                "html_url": str(row.get("url") or ""),
            },
        },
    )


def _persisted_findings(result: Any) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for finding in result.persisted_findings:
        status = str(getattr(finding, "status", ""))
        rows.append(
            {
                "finding_uid": finding.finding_uid,
                "fingerprint": finding.fingerprint,
                "trace_id": finding.trace_id,
                "agent": finding.agent,
                "raw_finding_id": finding.raw_finding_id,
                "status": status,
                "published": status == "published",
                "revision_of": finding.revision_of,
                "severity": finding.severity,
                "category": finding.category,
                "file": finding.file,
                "line": finding.line,
                "title": finding.title,
                "description": finding.description,
                "suggestion": finding.suggestion,
                "confidence": finding.confidence,
                "evidence": list(finding.evidence),
                "source_agents": list(finding.source_agents),
                "evidence_verified": finding.evidence_verified,
                "critic_decision": finding.critic_decision,
                "critic_reason": finding.critic_reason,
                "disposition": "",
                "reason_code": "",
                "label_source": "",
                "model_prelabel": None,
            }
        )
    return tuple(rows)


async def _apply_model_prelabelled_rows(
    rows: tuple[dict[str, Any], ...],
    *,
    llm: Any,
    model_id: str,
    diff: str,
) -> tuple[dict[str, Any], ...]:
    """Attach an independent model's tentative label without filling human fields."""
    published = [row for row in rows if row.get("published") is True]
    if not published:
        return rows
    result: list[dict[str, Any]] = []
    for row in rows:
        if row.get("published") is not True:
            result.append({**row, "model_prelabel": row.get("model_prelabel")})
            continue
        prompt = json.dumps(
            {
                "finding": {
                    key: row.get(key)
                    for key in (
                        "finding_uid",
                        "severity",
                        "category",
                        "file",
                        "line",
                        "title",
                        "description",
                        "suggestion",
                        "evidence",
                    )
                },
                "diff": diff[:12000],
            },
            ensure_ascii=False,
        )
        raw = await llm.chat(
            [
                {"role": "system", "content": PRELABEL_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        actual_model = str(
            (getattr(llm, "last_metrics", {}) or {}).get("model_used") or ""
        )
        if actual_model and actual_model != model_id:
            raise RuntimeError(
                f"model prelabel drift: expected {model_id!r}, observed {actual_model!r}"
            )
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid model prelabel JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise TypeError("model prelabel must be an object")
        disposition = str(payload.get("disposition") or "").strip()
        reason_code = str(payload.get("reason_code") or "").strip()
        if disposition not in {
            "accepted_actionable",
            "accepted_non_actionable",
            "false_positive",
            "duplicate",
            "already_known",
        }:
            raise ValueError(f"unsupported model prelabel disposition: {disposition}")
        if (
            disposition in {"accepted_non_actionable", "false_positive"}
            and not reason_code
        ):
            raise ValueError(
                f"model prelabel reason_code is required for {disposition}"
            )
        result.append(
            {
                **row,
                "model_prelabel": {
                    "disposition": disposition,
                    "reason_code": reason_code,
                    "rationale": str(payload.get("rationale") or "").strip(),
                    "model_id": model_id,
                },
            }
        )
    return tuple(result)


def _validate_fixed_model_usage(configured_model: str, models_used: Iterable[str]) -> None:
    actual = sorted({str(model) for model in models_used if str(model)})
    expected = [configured_model] if configured_model else []
    if actual != expected:
        raise RuntimeError(
            "evaluation model drift: expected exactly "
            f"{expected!r}, observed {actual!r}; fallback is not allowed"
        )


async def _run_case(
    *,
    arm: str,
    case_id: str,
    base_sha: str,
    head_sha: str,
    improvement_code: str,
    changed_path: str,
) -> ReviewRunRecord:
    os.environ["CODE_REVIEW_TEMPERATURE"] = "0"
    config = LLMConfig.from_env("CODE_REVIEW")
    token_budget = int(os.getenv("CODE_REVIEW_EVAL_TOKEN_BUDGET", "12000"))
    workspace_budget_seconds = int(
        os.getenv("CODE_REVIEW_EVAL_WORKSPACE_BUDGET_SECONDS", "300")
    )
    if token_budget <= 0 or workspace_budget_seconds <= 0:
        raise ValueError("evaluation token/workspace budgets must be positive")
    metered = MeteredLLM(build_code_review_llm(), token_budget=token_budget)
    critic = ReviewCriticAgent(metered) if arm != "R0" else None
    controlled = {case.case_id: case for case in load_controlled_cases()}
    real = {str(row["pr_id"]): row for row in load_pr_corpus()}
    github = build_github_client()
    grader_model = os.getenv("CODE_REVIEW_EVAL_GRADER_MODEL", "").strip()
    grader_llm: LLMClient | None = None
    if case_id in real and not isinstance(github, GitHubClient):
        close = getattr(github, "aclose", None)
        if close is not None:
            await close()
        await metered.aclose()
        raise RuntimeError(
            "public-PR evaluation requires GITHUB_TOKEN; refusing fixture review data"
        )
    pipeline = CodeReviewPipeline(
        github_client=github,
        workspace_provisioner=GitHubArchiveWorkspaceProvisioner(github),
        review_critic=critic,
        critic_enabled=arm != "R0",
        llm=metered,
    )

    started = time.monotonic()
    try:
        async def _review() -> Any:
            if case_id in controlled:
                pr, event = _controlled_pr(controlled[case_id])
                _, reviewed = await pipeline.run_prepared_pr(event, pr)
                return reviewed
            if case_id in real:
                event = _github_event(real[case_id], arm)
                _, reviewed = await pipeline.run(event)
                return reviewed
            raise ValueError(f"unknown evaluation case: {case_id}")

        try:
            result = await asyncio.wait_for(
                _review(),
                timeout=workspace_budget_seconds,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                "evaluation workspace budget exceeded: "
                f"{workspace_budget_seconds}s for {case_id}/{arm}"
            ) from exc
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        _validate_fixed_model_usage(config.model, metered.usage["models_used"])
        findings = _persisted_findings(result)
        if grader_model:
            grader_config = LLMConfig(**asdict(config))
            grader_config.model = grader_model
            grader_config.fallback_models = []
            grader_config.temperature = 0.0
            grader_llm = LLMClient(grader_config)
            findings = await _apply_model_prelabelled_rows(
                findings,
                llm=grader_llm,
                model_id=grader_model,
                diff=_review_diff(result),
            )
    finally:
        if grader_llm is not None:
            await grader_llm.aclose()
        close = getattr(github, "aclose", None)
        if close is not None:
            await close()
        await metered.aclose()

    critic_cost = float((result.critic_metrics or {}).get("cost_usd", 0.0) or 0.0)
    return ReviewRunRecord(
        case_id=case_id,
        arm=arm,
        findings=findings,
        known_defects_total=1 if case_id in controlled else 0,
        cost_usd=round(float(metered.usage["cost_usd"]), 6),
        critic_cost_usd=round(critic_cost, 6),
        latency_ms=latency_ms,
        model_id=config.model,
        temperature=0.0,
        token_budget=token_budget,
        workspace_budget_seconds=workspace_budget_seconds,
        base_sha=base_sha,
        head_sha=head_sha,
        improvement_code=improvement_code if arm == "R1+I" else "",
        changed_paths=(changed_path,) if arm == "R1+I" else (),
    )


def _review_diff(result: Any) -> str:
    changed_files = getattr(getattr(result, "pr", None), "changed_files", ())
    return "\n".join(
        str(getattr(file, "patch", "") or "")
        for file in changed_files
        if str(getattr(file, "patch", "") or "").strip()
    )


async def execute_matrix(
    *,
    arms: Iterable[str],
    case_ids: Iterable[str] | None = None,
    improvement_code: str = "",
    changed_path: str = "",
    improvement_base_ref: str = "",
    run_case: EvalRunner = _run_case,
) -> tuple[ReviewRunRecord, ...]:
    """Execute every selected arm/case pair with one stable protocol."""
    selected_arms = tuple(arms)
    unknown_arms = sorted(set(selected_arms) - {"R0", "R1", "R1+I"})
    if unknown_arms:
        raise ValueError(f"unsupported evaluation arms: {unknown_arms}")
    if "R1+I" in selected_arms and (
        not improvement_code or not changed_path or not improvement_base_ref
    ):
        raise ValueError(
            "R1+I requires one improvement code, one changed path, and a baseline ref"
        )
    if "R1+I" in selected_arms:
        _validate_improvement_scope(changed_path, improvement_base_ref)

    real_rows = load_pr_corpus()
    controlled_rows = load_controlled_cases()
    shuffled = {
        str(row["pr_id"]): (str(row["base_sha"]), str(row["head_sha"]))
        for row in real_rows
    }
    for case in controlled_rows:
        payload = controlled_review_input(case)
        shuffled[case.case_id] = (
            str(payload["base_commit"]),
            str(payload["head_sha"]),
        )
    selected_cases = tuple(case_ids or shuffled)
    unknown_cases = sorted(set(selected_cases) - set(shuffled))
    if unknown_cases:
        raise ValueError(f"unknown evaluation cases: {unknown_cases}")

    records: list[ReviewRunRecord] = []
    for arm in selected_arms:
        for case_id in selected_cases:
            base_sha, head_sha = shuffled[case_id]
            records.append(
                await run_case(
                    arm,
                    case_id,
                    base_sha,
                    head_sha,
                    improvement_code,
                    changed_path,
                )
            )
    return tuple(records)


def _validate_improvement_scope(changed_path: str, base_ref: str) -> None:
    """Prove R1+I differs from the R1 baseline in exactly one declared path."""
    result = subprocess.run(
        ["git", "diff", "--name-only", base_ref, "--"],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown git error"
        raise ValueError(f"invalid improvement baseline ref {base_ref!r}: {detail}")
    paths = {
        item.strip().replace("\\", "/")
        for item in result.stdout.splitlines()
        if item.strip()
    }
    declared = changed_path.strip().replace("\\", "/")
    if paths != {declared}:
        raise ValueError(
            "R1+I must change exactly one declared path relative to its baseline: "
            f"declared={declared!r}, actual={sorted(paths)}"
        )


def write_run_records(
    path: Path,
    records: Iterable[ReviewRunRecord],
    *,
    replace_existing: bool = False,
) -> None:
    existing: list[dict[str, Any]] = []
    if path.exists() and not replace_existing:
        payload = json.loads(path.read_text(encoding="utf-8"))
        existing = list(payload.get("runs", ()))
    incoming = [asdict(record) for record in records]
    keys = {(item["arm"], item["case_id"]) for item in incoming}
    retained = [
        item
        for item in existing
        if (str(item.get("arm")), str(item.get("case_id"))) not in keys
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"runs": [*retained, *incoming]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
