# Governed Repository Delivery Agent S1 Verifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the preregistered S1 topology: the same S0 Implementer plus one independent, read-only Verifier that can approve or request exactly one revision, without gaining write, GitHub, approval, or merge authority.

**Architecture:** `DeliveryFlow` gains an optional `VerifierPort` and enables it only for `topology="verified"`. Both production and experiment adapters reuse an existing tool loop, but the Verifier receives a physically read-only `ToolRegistry`; strict JSON parsing and bundle-hash validation turn malformed or stale reviews into fail-closed `VERIFIER_INVALID` results. A separate `bench/delivery/` harness compares S0 and S1 on the same model, budget, task set, hidden judge, and protocol hash.

**Tech Stack:** Python 3.12, frozen dataclasses, `typing.Protocol`, asyncio, existing `execute_tool_loop`, `bench.agent.LLMBackedAgent`, pytest, Ruff, existing Git workspace materialization.

**Spec:** `docs/superpowers/specs/2026-10-27-governed-repo-delivery-agent-design.md`

**Prerequisite:** Complete `docs/superpowers/plans/2026-10-27-governed-repo-delivery-agent-s0.md` first. S1 modifies the S0 files and assumes all S0 tests pass.

## Global Constraints

- Default topology remains `single`; S1 is enabled only by an explicit `topology="verified"` policy.
- Missing, malformed, stale, or failed Verifier output never falls back to S0.
- Implementer and Verifier use independent sessions.
- Verifier tools are exactly `read_file`, `search`, and `run_tests` when a verify command exists.
- Verifier code has no `write_file`, GitHub client, approval, merge-store, or merge-executor dependency.
- `approved` only advances to `READY_FOR_PUSH_APPROVAL`; it never pushes or merges.
- Test failure and Verifier `revise` share the single `max_revisions=1` budget.
- A second `revise` enters `NEEDS_HUMAN` with `VERIFIER_REVISE_EXHAUSTED`; no third Implementer call occurs.
- S0 and S1 use the same task set, model, temperature, per-run step/token budget, workspace materialization, verification command, and hidden judge.
- The Verifier consumes the run's remaining step/token budget; roles do not receive separate budgets.
- Product code under `apps/code_review_pipeline/delivery/` and `app/agents/` must not import `bench/`. Only `delivery/adapters/bench_agent.py` may import `bench/`.
- Hidden tests are never copied into the workspace before the Implementer or Verifier finishes.
- Task fixture patches use LF line endings; the runner applies them with
  `--ignore-space-change` so Windows editors cannot turn a one-line defect into
  an unapplied patch.
- Old v5/v8 multi-agent results are not evidence for S1 and must not enter the S1 report.
- The real repository-delivery CLI still requires explicit push enablement, a shared Postgres `MergeStore`, and human approval.
- Do not include `.scratch/*` in any commit.

## File Structure

**Create**

- `apps/code_review_pipeline/delivery/verification.py`
- `apps/code_review_pipeline/delivery/adapters/production_verifier.py`
- `apps/code_review_pipeline/delivery/adapters/bench_agent.py`
- `bench/delivery/__init__.py`
- `bench/delivery/models.py`
- `bench/delivery/protocol.py`
- `bench/delivery/runner.py`
- `bench/delivery/report.py`
- `bench/delivery/protocols/repository-delivery-s0-s1-v1.json`
- `bench/delivery/tasks/feishu-post-rich-text/task.json`
- `bench/delivery/tasks/feishu-post-rich-text/visible/tests/delivery_visible/test_feishu_post.py`
- `bench/delivery/tasks/feishu-post-rich-text/hidden/tests/delivery_hidden/test_feishu_post_hidden.py`
- `bench/delivery/tasks/budget-ttl-refresh/task.json`
- `bench/delivery/tasks/budget-ttl-refresh/visible/defect.patch`
- `bench/delivery/tasks/budget-ttl-refresh/visible/tests/delivery_visible/test_budget_ttl.py`
- `bench/delivery/tasks/budget-ttl-refresh/hidden/tests/delivery_hidden/test_budget_ttl_hidden.py`
- `bench/delivery/tasks/keyword-normalization/task.json`
- `bench/delivery/tasks/keyword-normalization/visible/defect.patch`
- `bench/delivery/tasks/keyword-normalization/visible/tests/delivery_visible/test_keyword_normalization.py`
- `bench/delivery/tasks/keyword-normalization/hidden/tests/delivery_hidden/test_keyword_normalization_hidden.py`
- `bench/delivery/tasks/binary-write-review/task.json`
- `bench/delivery/tasks/binary-write-review/visible/defect.patch`
- `bench/delivery/tasks/binary-write-review/visible/tests/delivery_visible/test_binary_write_review.py`
- `bench/delivery/tasks/binary-write-review/hidden/tests/delivery_hidden/test_binary_write_review_hidden.py`
- `bench/run_delivery_experiment.py`
- `tests/unit/test_delivery_verification.py`
- `tests/unit/test_delivery_verifier_adapter.py`
- `tests/unit/test_delivery_flow_verified.py`
- `tests/unit/test_delivery_bench_adapter.py`
- `tests/unit/test_delivery_experiment.py`
- `tests/unit/test_delivery_protocol.py`

**Modify**

- `bench/agent/loop.py`
- `apps/code_review_pipeline/delivery/models.py`
- `apps/code_review_pipeline/delivery/ports.py`
- `apps/code_review_pipeline/delivery/flow.py`
- `apps/code_review_pipeline/delivery/__init__.py`
- `apps/code_review_pipeline/delivery/adapters/__init__.py`
- `app/cli.py`
- `README.md`
- `CONTEXT.md`
- `delivery/定位与减法清单.md`
- `docs/adr/adr-031-governed-repository-delivery-agent.md`
- `tests/unit/test_bench_loop_session.py`
- `tests/unit/test_delivery_import_boundaries.py`

**Responsibility map**

- `verification.py`: strict Verifier JSON parsing, evidence checks, prompt construction.
- `production_verifier.py`: product adapter over the shared structured tool loop.
- `bench_agent.py`: experiment-only mapping from bench `AgentRun` to product attempt/verdict types.
- `bench/delivery/models.py`: experiment task, trial, and report contracts.
- `bench/delivery/protocol.py`: frozen task-set hashing and protocol validation.
- `bench/delivery/runner.py`: visible fail-first, S0/S1 flow execution, hidden judge.
- `bench/delivery/report.py`: paired S0/S1 metrics and pre-registration gates.
- `bench/run_delivery_experiment.py`: offline task verification and live pilot/formal commands.

---

### Task 1: Add the Verifier Port, Verdict Validation, and Strict JSON Parser

**Files:**
- Modify: `apps/code_review_pipeline/delivery/models.py`
- Modify: `apps/code_review_pipeline/delivery/ports.py`
- Create: `apps/code_review_pipeline/delivery/verification.py`
- Modify: `apps/code_review_pipeline/delivery/__init__.py`
- Test: `tests/unit/test_delivery_verification.py`

**Interfaces:**
- Consumes: `AgentAttempt`, `ChangeBundle`, `EvidenceRef`, `VerifierVerdict`.
- Produces: `VerifierPort.review(...) -> VerifierVerdict`, `VerifierInvalid`, `parse_verifier_output(text, *, attempt, bundle) -> VerifierVerdict`, `validate_verdict(verdict, bundle) -> None`, `build_verifier_messages(...) -> list[dict[str, str]]`.

- [ ] **Step 1: Write the failing parser and verdict tests**

Create `tests/unit/test_delivery_verification.py`.

```python
from __future__ import annotations

import json

import pytest

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    AgentStep,
    DeliveryUsage,
    EvidenceRef,
    VerifierVerdict,
)
from apps.code_review_pipeline.delivery.ports import VerifierInvalid
from apps.code_review_pipeline.delivery.verification import (
    build_verifier_messages,
    parse_verifier_output,
    validate_verdict,
)


def _bundle() -> ChangeBundle:
    return ChangeBundle(
        base_sha="a" * 40,
        branch="moa/fix-trace-1",
        title="fix",
        body="body",
        files=(),
        patch="diff --git a/app.py b/app.py\n",
        patch_hash="p" * 64,
        verify_command=("pytest", "-q"),
        verify_exit_code=0,
        verify_output="1 passed",
        model="test-model",
        trace_id="trace-1",
    )


def _attempt() -> AgentAttempt:
    return AgentAttempt(
        role="verifier",
        status="completed",
        summary="reviewed",
        stop_reason="model finished",
        usage=DeliveryUsage(llm_calls=1, prompt_tokens=10, completion_tokens=5),
        steps=(
            AgentStep(
                index=1,
                action="call_tool",
                tool="read_file",
                arguments={"path": "app.py"},
                observation="1: def add(a, b):",
                outcome="ok",
                elapsed_ms=1.0,
            ),
        ),
    )


def _approved_payload() -> str:
    return json.dumps(
        {
            "decision": "approved",
            "reason": "the fix handles the target behavior",
            "instructions": "",
            "evidence": [
                {
                    "kind": "file",
                    "ref": "app.py",
                    "observation": "add returns a + b",
                }
            ],
        }
    )


def test_parser_attaches_the_current_bundle_hashes() -> None:
    bundle = _bundle()

    verdict = parse_verifier_output(
        _approved_payload(),
        attempt=_attempt(),
        bundle=bundle,
    )

    assert verdict.decision == "approved"
    assert verdict.reviewed_bundle_hash == bundle.bundle_hash
    assert verdict.reviewed_patch_hash == bundle.patch_hash
    assert verdict.evidence[0].ref == "app.py"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not json", "strict JSON"),
        (
            json.dumps(
                {
                    "decision": "approved",
                    "reason": "ok",
                    "instructions": "",
                    "evidence": [],
                }
            ),
            "evidence",
        ),
        (
            json.dumps(
                {
                    "decision": "revise",
                    "reason": "bad",
                    "instructions": " ",
                    "evidence": [],
                }
            ),
            "instructions",
        ),
        (
            json.dumps(
                {
                    "decision": "pass",
                    "reason": "ok",
                    "instructions": "",
                    "evidence": [
                        {
                            "kind": "file",
                            "ref": "app.py",
                            "observation": "ok",
                        }
                    ],
                }
            ),
            "decision",
        ),
    ],
)
def test_parser_is_fail_closed(payload: str, message: str) -> None:
    with pytest.raises(VerifierInvalid, match=message):
        parse_verifier_output(payload, attempt=_attempt(), bundle=_bundle())


def test_validate_rejects_a_stale_bundle_hash() -> None:
    bundle = _bundle()
    verdict = VerifierVerdict(
        decision="approved",
        reason="ok",
        instructions="",
        evidence=(EvidenceRef(kind="file", ref="app.py", observation="ok"),),
        reviewed_bundle_hash="old",
        reviewed_patch_hash=bundle.patch_hash,
        attempt=_attempt(),
    )

    with pytest.raises(VerifierInvalid, match="bundle hash"):
        validate_verdict(verdict, bundle)


def test_prompt_contains_the_goal_bundle_hashes_and_strict_shape() -> None:
    messages = build_verifier_messages(
        workspace="/tmp/workspace",
        goal="fix add",
        bundle=_bundle(),
        implementer_attempt=AgentAttempt(
            role="implementer",
            status="completed",
            summary="changed app.py",
            stop_reason="model finished",
            usage=DeliveryUsage(),
            steps=(),
        ),
    )
    text = "\n".join(item["content"] for item in messages)

    assert "fix add" in text
    assert _bundle().bundle_hash in text
    assert _bundle().patch_hash in text
    assert '"decision": "approved" | "revise"' in text
    assert "write_file" not in text
```

- [ ] **Step 2: Run the parser tests to verify they fail**

Run:

```powershell
uv run pytest tests/unit/test_delivery_verification.py -q
```

Expected: FAIL because `delivery.verification` and `VerifierInvalid` do not exist.

- [ ] **Step 3: Enforce verdict invariants in the model**

Add `__post_init__` to `VerifierVerdict` in `models.py`.

```python
    def __post_init__(self) -> None:
        if self.decision not in {"approved", "revise"}:
            raise ValueError(f"decision 非法: {self.decision!r}")
        if not self.reason.strip():
            raise ValueError("reason 不能为空")
        if self.attempt.role != "verifier":
            raise ValueError("verdict attempt role 必须是 verifier")
        if not self.reviewed_bundle_hash.strip():
            raise ValueError("reviewed_bundle_hash 不能为空")
        if not self.reviewed_patch_hash.strip():
            raise ValueError("reviewed_patch_hash 不能为空")
        if self.decision == "approved" and not self.evidence:
            raise ValueError("approved 至少需要一条 evidence")
        if self.decision == "revise" and not self.instructions.strip():
            raise ValueError("revise 必须给出非空 instructions")
```

- [ ] **Step 4: Add the verifier port and fail-closed exception**

Add to `ports.py`.

```python
from apps.code_review_pipeline.delivery.models import VerifierVerdict


class VerifierInvalid(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        attempt: AgentAttempt | None = None,
    ) -> None:
        super().__init__(message)
        self.attempt = attempt


class VerifierPort(Protocol):
    async def review(
        self,
        *,
        workspace: Path,
        goal: str,
        bundle: ChangeBundle,
        implementer_attempt: AgentAttempt,
        max_steps: int,
        max_tokens: int,
    ) -> VerifierVerdict: ...
```

- [ ] **Step 5: Implement strict parsing and validation**

Create `apps/code_review_pipeline/delivery/verification.py`.

````python
from __future__ import annotations

import json
from typing import Any

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    EvidenceRef,
    VerifierVerdict,
)
from apps.code_review_pipeline.delivery.ports import VerifierInvalid

_VERDICT_KEYS = {"decision", "reason", "instructions", "evidence"}
_EVIDENCE_KEYS = {"kind", "ref", "observation"}


def _invalid(message: str, attempt: AgentAttempt | None = None) -> VerifierInvalid:
    return VerifierInvalid(message, attempt=attempt)


def parse_verifier_output(
    text: str,
    *,
    attempt: AgentAttempt,
    bundle: ChangeBundle,
) -> VerifierVerdict:
    raw = (text or "").strip()
    if not raw or raw.startswith("```"):
        raise _invalid("Verifier must return one strict JSON object", attempt)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _invalid(f"Verifier output is not strict JSON: {exc}", attempt) from exc
    if not isinstance(payload, dict):
        raise _invalid("Verifier output must be a JSON object", attempt)
    if set(payload) != _VERDICT_KEYS:
        raise _invalid(
            "Verifier output fields must be exactly "
            "decision/reason/instructions/evidence",
            attempt,
        )

    decision = str(payload["decision"]).strip().lower()
    reason = str(payload["reason"]).strip()
    instructions = str(payload["instructions"]).strip()
    raw_evidence = payload["evidence"]
    if not isinstance(raw_evidence, list):
        raise _invalid("evidence must be an array", attempt)
    evidence: list[EvidenceRef] = []
    for item in raw_evidence:
        if not isinstance(item, dict) or set(item) != _EVIDENCE_KEYS:
            raise _invalid(
                "each evidence item must contain kind/ref/observation",
                attempt,
            )
        kind = str(item["kind"]).strip()
        ref = str(item["ref"]).strip()
        observation = str(item["observation"]).strip()
        if kind not in {"file", "command", "diff"}:
            raise _invalid(f"unsupported evidence kind: {kind!r}", attempt)
        if not ref or not observation:
            raise _invalid("evidence ref and observation must be non-empty", attempt)
        evidence.append(
            EvidenceRef(kind=kind, ref=ref, observation=observation)
        )

    try:
        verdict = VerifierVerdict(
            decision=decision,
            reason=reason,
            instructions=instructions,
            evidence=tuple(evidence),
            reviewed_bundle_hash=bundle.bundle_hash,
            reviewed_patch_hash=bundle.patch_hash,
            attempt=attempt,
        )
    except ValueError as exc:
        raise _invalid(str(exc), attempt) from exc
    validate_verdict(verdict, bundle)
    return verdict


def validate_verdict(
    verdict: VerifierVerdict,
    bundle: ChangeBundle,
) -> None:
    if verdict.attempt.status != "completed":
        raise _invalid(
            f"Verifier attempt status is {verdict.attempt.status!r}",
            verdict.attempt,
        )
    if verdict.reviewed_bundle_hash != bundle.bundle_hash:
        raise _invalid(
            "Verifier reviewed a stale bundle hash",
            verdict.attempt,
        )
    if verdict.reviewed_patch_hash != bundle.patch_hash:
        raise _invalid(
            "Verifier reviewed a stale patch hash",
            verdict.attempt,
        )
    if verdict.decision == "approved" and not verdict.evidence:
        raise _invalid("approved requires evidence", verdict.attempt)
    if verdict.decision == "revise" and not verdict.instructions.strip():
        raise _invalid("revise requires instructions", verdict.attempt)


def build_verifier_messages(
    *,
    workspace: str,
    goal: str,
    bundle: ChangeBundle,
    implementer_attempt: AgentAttempt,
) -> list[dict[str, str]]:
    trajectory = json.dumps(
        implementer_attempt.to_dict(),
        ensure_ascii=False,
        indent=2,
    )[:30_000]
    system = (
        "You are the read-only Verifier in a governed repository delivery "
        "pipeline. You cannot write files, access GitHub, approve, or merge. "
        "Inspect the workspace and run the fixed verification command when useful. "
        "Return exactly one JSON object and no markdown fence."
    )
    user = (
        f"Workspace: {workspace}\n"
        f"Goal:\n{goal}\n\n"
        f"Bundle hash: {bundle.bundle_hash}\n"
        f"Patch hash: {bundle.patch_hash}\n"
        f"Patch excerpt:\n{bundle.patch[:20_000]}\n\n"
        f"Implementer attempt:\n{trajectory}\n\n"
        "Required JSON shape:\n"
        '{"decision": "approved" | "revise", '
        '"reason": "short reason", '
        '"instructions": "non-empty only for revise", '
        '"evidence": [{"kind": "file" | "command" | "diff", '
        '"ref": "path or command", "observation": "what you verified"}]}\n\n'
        "An approved verdict needs at least one evidence item. "
        "A revise verdict needs concrete file/line/test instructions. "
        "Use run_tests yourself; do not trust the Implementer summary."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
````

- [ ] **Step 6: Export and run the tests**

Add `VerifierInvalid`, `VerifierPort`, `parse_verifier_output`, `validate_verdict`,
and `build_verifier_messages` to the delivery package `__all__` list.

Run:

```powershell
uv run pytest tests/unit/test_delivery_verification.py tests/unit/test_delivery_models.py -q
uv run ruff check apps/code_review_pipeline/delivery tests/unit/test_delivery_verification.py
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 7: Commit**

```powershell
git add apps/code_review_pipeline/delivery/models.py apps/code_review_pipeline/delivery/ports.py apps/code_review_pipeline/delivery/verification.py apps/code_review_pipeline/delivery/__init__.py tests/unit/test_delivery_verification.py
git commit -m "feat: add strict verifier verdict contract"
```

---

### Task 2: Implement the Production Verifier Adapter

**Files:**
- Create: `apps/code_review_pipeline/delivery/adapters/production_verifier.py`
- Modify: `apps/code_review_pipeline/delivery/adapters/__init__.py`
- Test: `tests/unit/test_delivery_verifier_adapter.py`

**Interfaces:**
- Consumes: `execute_tool_loop`, `build_toolset`, `build_verifier_messages`, `parse_verifier_output`, `VerifierPort`.
- Produces: `ProductionVerifier(llm, verify_command)` implementing `VerifierPort.review(...)`.

- [ ] **Step 1: Write the failing adapter tests**

Create `tests/unit/test_delivery_verifier_adapter.py`.

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.provider import ChatResult
from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.delivery.adapters.production_verifier import (
    ProductionVerifier,
)
from apps.code_review_pipeline.delivery.models import AgentAttempt, DeliveryUsage
from apps.code_review_pipeline.delivery.ports import VerifierInvalid


def _bundle() -> ChangeBundle:
    return ChangeBundle(
        base_sha="a" * 40,
        branch="moa/fix-trace-1",
        title="fix",
        body="",
        files=(),
        patch="diff --git a/app.py b/app.py\n",
        patch_hash="p" * 64,
        verify_command=("pytest", "-q"),
        verify_exit_code=0,
        verify_output="1 passed",
        model="test-model",
        trace_id="trace-1",
    )


def _implementer_attempt() -> AgentAttempt:
    return AgentAttempt(
        role="implementer",
        status="completed",
        summary="changed app.py",
        stop_reason="model finished",
        usage=DeliveryUsage(),
        steps=(),
    )


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class FakeVerifierLLM:
    def __init__(self, final: str) -> None:
        self.final = final
        self.tool_names: list[list[str]] = []
        self.last_metrics = {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "cost_usd": 0.001,
            "llm_latency_ms": 3.0,
        }
        self._responses = [
            [_call("1", "read_file", {"path": "app.py"})],
            final,
        ]

    async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResult:
        self.tool_names.append(
            [item["function"]["name"] for item in tools]
        )
        response = self._responses.pop(0)
        if isinstance(response, str):
            return ChatResult(
                messages=[*messages, {"role": "assistant", "content": response}],
                content=response,
                tool_calls=[],
            )
        return ChatResult(
            messages=[*messages, {"role": "assistant", "content": None}],
            content="",
            tool_calls=response,
        )


@pytest.mark.asyncio
async def test_verifier_uses_read_only_tools_and_returns_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    final = json.dumps(
        {
            "decision": "approved",
            "reason": "correct implementation",
            "instructions": "",
            "evidence": [
                {
                    "kind": "file",
                    "ref": "app.py",
                    "observation": "add returns a + b",
                }
            ],
        }
    )
    llm = FakeVerifierLLM(final)
    verifier = ProductionVerifier(
        llm=llm,
        verify_command=("python", "-m", "pytest", "-q"),
    )

    verdict = await verifier.review(
        workspace=tmp_path,
        goal="fix add",
        bundle=_bundle(),
        implementer_attempt=_implementer_attempt(),
        max_steps=5,
        max_tokens=1000,
    )

    assert verdict.decision == "approved"
    assert verdict.attempt.role == "verifier"
    assert verdict.attempt.usage.prompt_tokens == 40
    assert set(llm.tool_names[0]) == {"read_file", "search", "run_tests"}
    assert "write_file" not in llm.tool_names[0]


@pytest.mark.asyncio
async def test_verifier_rejects_an_approved_answer_without_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    final = json.dumps(
        {
            "decision": "approved",
            "reason": "looks fine",
            "instructions": "",
            "evidence": [],
        }
    )
    verifier = ProductionVerifier(
        llm=FakeVerifierLLM(final),
        verify_command=("python", "-c", "raise SystemExit(0)"),
    )

    with pytest.raises(VerifierInvalid, match="evidence") as caught:
        await verifier.review(
            workspace=tmp_path,
            goal="fix add",
            bundle=_bundle(),
            implementer_attempt=_implementer_attempt(),
            max_steps=5,
            max_tokens=1000,
        )

    assert caught.value.attempt is not None
    assert caught.value.attempt.role == "verifier"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run pytest tests/unit/test_delivery_verifier_adapter.py -q
```

Expected: FAIL because `ProductionVerifier` does not exist.

- [ ] **Step 3: Implement the production adapter**

Create `apps/code_review_pipeline/delivery/adapters/production_verifier.py`.

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.stubs import execute_tool_loop
from app.agents.workspace_tools import build_toolset
from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    AgentStep,
    DeliveryUsage,
)
from apps.code_review_pipeline.delivery.ports import VerifierInvalid
from apps.code_review_pipeline.delivery.verification import (
    build_verifier_messages,
    parse_verifier_output,
)


class ProductionVerifier:
    def __init__(
        self,
        *,
        llm: Any,
        verify_command: tuple[str, ...],
    ) -> None:
        self._llm = llm
        self._verify_command = tuple(verify_command)

    async def review(
        self,
        *,
        workspace: Path,
        goal: str,
        bundle: ChangeBundle,
        implementer_attempt: AgentAttempt,
        max_steps: int,
        max_tokens: int,
    ):
        root = Path(workspace).resolve()
        names = ["read_file", "search"]
        if self._verify_command:
            names.append("run_tests")
        tools = build_toolset(
            workspace=root,
            verify_command=self._verify_command,
            names=tuple(names),
        )
        result = await execute_tool_loop(
            llm=self._llm,
            messages=build_verifier_messages(
                workspace=str(root),
                goal=goal,
                bundle=bundle,
                implementer_attempt=implementer_attempt,
            ),
            tools=tools,
            workspace=root,
            agent_name="delivery_verifier",
            max_steps=max_steps,
            max_tokens=max_tokens,
        )
        attempt = AgentAttempt(
            role="verifier",
            status=result.status,
            summary=result.content.strip()[:2000],
            stop_reason=result.stop_reason,
            usage=DeliveryUsage(
                llm_calls=result.usage.llm_calls,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cost_usd=result.usage.cost_usd,
                llm_latency_ms=result.usage.llm_latency_ms,
            ),
            steps=tuple(
                AgentStep(
                    index=step.index,
                    action=step.action,
                    tool=step.tool,
                    arguments=dict(step.arguments),
                    observation=step.observation,
                    outcome=step.outcome,
                    elapsed_ms=step.elapsed_ms,
                )
                for step in result.steps
            ),
        )
        if result.status != "completed":
            raise VerifierInvalid(
                f"Verifier stopped with status {result.status}: {result.stop_reason}",
                attempt=attempt,
            )
        return parse_verifier_output(
            result.content,
            attempt=attempt,
            bundle=bundle,
        )
```

Update `adapters/__init__.py`.

```python
from apps.code_review_pipeline.delivery.adapters.production_verifier import (
    ProductionVerifier,
)

__all__ = ["GitHubChangeAdapter", "ProductionImplementer", "ProductionVerifier"]
```

- [ ] **Step 4: Run the adapter tests**

Run:

```powershell
uv run pytest tests/unit/test_delivery_verifier_adapter.py tests/unit/test_structured_tool_loop.py -q
uv run ruff check apps/code_review_pipeline/delivery/adapters tests/unit/test_delivery_verifier_adapter.py
```

Expected: all tests pass; the captured schema list proves `write_file` is absent.

- [ ] **Step 5: Commit**

```powershell
git add apps/code_review_pipeline/delivery/adapters/production_verifier.py apps/code_review_pipeline/delivery/adapters/__init__.py tests/unit/test_delivery_verifier_adapter.py
git commit -m "feat: add read-only production verifier"
```

---

### Task 3: Enable the Verified Topology in `DeliveryFlow`

**Files:**
- Modify: `apps/code_review_pipeline/delivery/flow.py`
- Modify: `apps/code_review_pipeline/delivery/ports.py`
- Modify: `apps/code_review_pipeline/delivery/__init__.py`
- Test: `tests/unit/test_delivery_flow_verified.py`

**Interfaces:**
- Consumes: `VerifierPort`, `VerifierInvalid`, `validate_verdict`, S0 `DeliveryFlow`.
- Produces: `DeliveryFlow(..., verifier: VerifierPort | None = None)` and complete S1 orchestration.

- [ ] **Step 1: Write the failing verified-flow tests**

Create `tests/unit/test_delivery_flow_verified.py`.

```python
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.pipeline import ChangeApproval
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.delivery.flow import DeliveryFlow
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRunState,
    DeliveryUsage,
    EvidenceRef,
    FailureCode,
    PushOutcome,
    VerificationEvidence,
    VerifierVerdict,
)
from apps.code_review_pipeline.delivery.ports import (
    ChangePreparation,
    PreparationStatus,
    VerifierInvalid,
)


def _bundle() -> ChangeBundle:
    return ChangeBundle(
        base_sha="a" * 40,
        branch="moa/fix-trace-1",
        title="fix",
        body="",
        files=(),
        patch="diff --git a/app.py b/app.py\n",
        patch_hash="p" * 64,
        verify_command=("pytest", "-q"),
        verify_exit_code=0,
        verify_output="1 passed",
        model="test-model",
        trace_id="trace-1",
    )


def _attempt(role: str, summary: str) -> AgentAttempt:
    return AgentAttempt(
        role=role,
        status="completed",
        summary=summary,
        stop_reason="model finished",
        usage=DeliveryUsage(
            llm_calls=1,
            prompt_tokens=10,
            completion_tokens=5,
        ),
        steps=(),
    )


class FakeImplementer:
    def __init__(self, attempts: list[AgentAttempt]) -> None:
        self.attempts = list(attempts)
        self.feedback: list[str] = []

    async def run(self, **kwargs):
        self.feedback.append(str(kwargs["feedback"]))
        return self.attempts.pop(0)


class FakePreparer:
    def __init__(self, bundle: ChangeBundle) -> None:
        self.bundle = bundle
        self.calls = 0

    async def prepare(self, **kwargs):
        self.calls += 1
        return ChangePreparation(
            status=PreparationStatus.READY,
            bundle=self.bundle,
            verification=VerificationEvidence(
                command=self.bundle.verify_command,
                exit_code=0,
                output_hash="o" * 64,
                output_excerpt="1 passed",
            ),
        )


class FakeVerifier:
    def __init__(self, verdicts: list[VerifierVerdict | Exception]) -> None:
        self.verdicts = list(verdicts)
        self.calls = 0

    async def review(self, **kwargs):
        self.calls += 1
        value = self.verdicts.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeApproval:
    def __init__(self, bundle: ChangeBundle) -> None:
        self.bundle = bundle
        self.calls = 0

    async def request(self, **kwargs):
        self.calls += 1
        return ChangeApproval(
            approver="alice",
            trace_id="trace-1",
            bundle_hash=self.bundle.bundle_hash,
        )


class FakePublisher:
    def __init__(self) -> None:
        self.calls = 0

    async def publish(self, **kwargs) -> PushOutcome:
        self.calls += 1
        return PushOutcome(
            branch="moa/fix-trace-1",
            commit_sha="b" * 40,
            pull_request=PullRequestRef(
                number=9,
                url="https://example.com/pr/9",
                head_sha="b" * 40,
            ),
        )


class Recorder:
    async def save(self, run) -> None:
        return None


class Audit:
    async def record(self, **kwargs) -> None:
        return None


def _request() -> DeliveryRequest:
    return DeliveryRequest(
        trace_id="trace-1",
        repo="owner/name",
        base_sha="a" * 40,
        base_branch="main",
        branch="moa/fix",
        goal="fix add",
        title="fix",
        body="",
    )


def _verdict(decision: str, bundle: ChangeBundle, instructions: str = "") -> VerifierVerdict:
    return VerifierVerdict(
        decision=decision,
        reason="reviewed",
        instructions=instructions,
        evidence=(EvidenceRef(kind="file", ref="app.py", observation="checked"),),
        reviewed_bundle_hash=bundle.bundle_hash,
        reviewed_patch_hash=bundle.patch_hash,
        attempt=_attempt("verifier", "reviewed"),
    )


def _flow(
    tmp_path: Path,
    *,
    implementer: FakeImplementer,
    preparer: FakePreparer,
    verifier: FakeVerifier,
    approval: FakeApproval,
    publisher: FakePublisher,
) -> DeliveryFlow:
    return DeliveryFlow(
        workspace=tmp_path,
        policy=DeliveryPolicy(
            verify_command=("pytest", "-q"),
            topology="verified",
        ),
        implementer=implementer,
        preparer=preparer,
        verifier=verifier,
        approval=approval,
        publisher=publisher,
        recorder=Recorder(),
        audit=Audit(),
        clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
        new_run_id=lambda: "run-1",
    )


@pytest.mark.asyncio
async def test_approved_verifier_reaches_the_push_gate(tmp_path: Path) -> None:
    bundle = _bundle()
    approval = FakeApproval(bundle)
    verifier = FakeVerifier([_verdict("approved", bundle)])
    flow = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt("implementer", "changed")]),
        preparer=FakePreparer(bundle),
        verifier=verifier,
        approval=approval,
        publisher=FakePublisher(),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.WAITING_CI
    assert verifier.calls == 1
    assert approval.calls == 1
    assert run.verifier_verdicts[0].decision == "approved"
    assert [item.role for item in run.attempts] == ["implementer", "verifier"]


@pytest.mark.asyncio
async def test_one_revise_runs_exactly_one_implementer_revision(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    implementer = FakeImplementer(
        [
            _attempt("implementer", "first"),
            _attempt("implementer", "second"),
        ]
    )
    verifier = FakeVerifier(
        [
            _verdict("revise", bundle, "handle the zero case"),
            _verdict("approved", bundle),
        ]
    )
    flow = _flow(
        tmp_path,
        implementer=implementer,
        preparer=FakePreparer(bundle),
        verifier=verifier,
        approval=FakeApproval(bundle),
        publisher=FakePublisher(),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.WAITING_CI
    assert implementer.feedback == ["", "handle the zero case"]
    assert verifier.calls == 2


@pytest.mark.asyncio
async def test_second_revise_stops_without_a_third_implementer(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    implementer = FakeImplementer(
        [
            _attempt("implementer", "first"),
            _attempt("implementer", "second"),
        ]
    )
    verifier = FakeVerifier(
        [
            _verdict("revise", bundle, "first correction"),
            _verdict("revise", bundle, "second correction"),
        ]
    )
    flow = _flow(
        tmp_path,
        implementer=implementer,
        preparer=FakePreparer(bundle),
        verifier=verifier,
        approval=FakeApproval(bundle),
        publisher=FakePublisher(),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure is not None
    assert run.failure.code is FailureCode.VERIFIER_REVISE_EXHAUSTED
    assert len(implementer.attempts) == 0


@pytest.mark.asyncio
async def test_invalid_verifier_fails_closed_without_s0_fallback(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    approval = FakeApproval(bundle)
    flow = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt("implementer", "changed")]),
        preparer=FakePreparer(bundle),
        verifier=FakeVerifier([VerifierInvalid("broken JSON")]),
        approval=approval,
        publisher=FakePublisher(),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure is not None
    assert run.failure.code is FailureCode.VERIFIER_INVALID
    assert approval.calls == 0


def test_verified_topology_requires_a_verifier(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="verifier"):
        DeliveryFlow(
            workspace=tmp_path,
            policy=DeliveryPolicy(
                verify_command=("pytest", "-q"),
                topology="verified",
            ),
            implementer=FakeImplementer([]),
            preparer=FakePreparer(_bundle()),
            approval=FakeApproval(_bundle()),
            publisher=FakePublisher(),
            recorder=Recorder(),
            audit=Audit(),
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run pytest tests/unit/test_delivery_flow_verified.py -q
```

Expected: FAIL because `DeliveryFlow` has no `verifier` parameter and rejects `verified`.

- [ ] **Step 3: Add verifier-aware construction**

Change `DeliveryFlow.__init__` to accept `verifier: VerifierPort | None = None`
and replace the S0-only topology guard with:

```python
        if policy.topology == "verified" and verifier is None:
            raise ValueError("verified topology requires a VerifierPort")
        self._verifier = verifier
```

- [ ] **Step 4: Insert the bounded verifier review after a ready bundle**

Replace the S0 block that immediately breaks after storing a ready bundle with
the following logic. `revisions` and `feedback` are the same variables used by
the S0 loop.

```python
                if self._policy.topology == "verified":
                    run = await self._move(
                        run,
                        DeliveryRunState.VERIFIER_REVIEW,
                        "verifier_review",
                    )
                    remaining_steps, remaining_tokens = self._remaining_budget(run)
                    if remaining_steps <= 0 or remaining_tokens <= 0:
                        return await self._finish(
                            run,
                            DeliveryRunState.BUDGET_EXHAUSTED,
                            None,
                        )
                    assert self._verifier is not None
                    try:
                        verdict = await self._verifier.review(
                            workspace=self._workspace,
                            goal=request.goal,
                            bundle=run.bundle,
                            implementer_attempt=attempt,
                            max_steps=remaining_steps,
                            max_tokens=remaining_tokens,
                        )
                    except VerifierInvalid as exc:
                        if exc.attempt is not None:
                            run = replace(
                                run,
                                attempts=(*run.attempts, exc.attempt),
                                usage=run.usage + exc.attempt.usage,
                                updated_at=self._clock(),
                            )
                            await self._recorder.save(run)
                        return await self._finish(
                            run,
                            DeliveryRunState.NEEDS_HUMAN,
                            FailureInfo(
                                FailureCode.VERIFIER_INVALID,
                                str(exc),
                            ),
                        )
                    except Exception as exc:
                        return await self._finish(
                            run,
                            DeliveryRunState.NEEDS_HUMAN,
                            FailureInfo(
                                FailureCode.VERIFIER_INVALID,
                                f"{type(exc).__name__}: {exc}",
                            ),
                        )
                    try:
                        validate_verdict(verdict, run.bundle)
                    except VerifierInvalid as exc:
                        run = replace(
                            run,
                            attempts=(*run.attempts, verdict.attempt),
                            verifier_verdicts=(*run.verifier_verdicts, verdict),
                            usage=run.usage + verdict.attempt.usage,
                            updated_at=self._clock(),
                        )
                        await self._recorder.save(run)
                        return await self._finish(
                            run,
                            DeliveryRunState.NEEDS_HUMAN,
                            FailureInfo(
                                FailureCode.VERIFIER_INVALID,
                                str(exc),
                            ),
                        )
                    run = replace(
                        run,
                        attempts=(*run.attempts, verdict.attempt),
                        verifier_verdicts=(*run.verifier_verdicts, verdict),
                        usage=run.usage + verdict.attempt.usage,
                        updated_at=self._clock(),
                    )
                    await self._recorder.save(run)
                    if run.usage.total_tokens >= self._policy.max_tokens:
                        return await self._finish(
                            run,
                            DeliveryRunState.BUDGET_EXHAUSTED,
                            None,
                        )
                    if verdict.decision == "approved":
                        break
                    if revisions >= self._policy.max_revisions:
                        return await self._finish(
                            run,
                            DeliveryRunState.NEEDS_HUMAN,
                            FailureInfo(
                                FailureCode.VERIFIER_REVISE_EXHAUSTED,
                                verdict.instructions,
                            ),
                        )
                    revisions += 1
                    feedback = verdict.instructions
                    run = await self._move(
                        run,
                        DeliveryRunState.REVISING,
                        "verifier_revise",
                    )
                    continue
                break
```

Import `VerifierPort` and `VerifierInvalid` from `ports.py`, and
`validate_verdict` from `verification.py`. Keep `run.bundle` and
`run.verification` bound to the exact bundle reviewed.

- [ ] **Step 5: Run S0 and S1 flow tests**

Run:

```powershell
uv run pytest tests/unit/test_delivery_flow.py tests/unit/test_delivery_flow_verified.py tests/unit/test_delivery_state.py -q
uv run ruff check apps/code_review_pipeline/delivery tests/unit/test_delivery_flow_verified.py
```

Expected: all S0 tests remain green; S1 tests prove approval, one revise,
second-revise stop, and fail-closed invalid output.

- [ ] **Step 6: Commit**

```powershell
git add apps/code_review_pipeline/delivery/flow.py apps/code_review_pipeline/delivery/ports.py apps/code_review_pipeline/delivery/__init__.py tests/unit/test_delivery_flow_verified.py
git commit -m "feat: add bounded verified delivery topology"
```

---

### Task 4: Add the Experiment-Only Bench Adapter

**Files:**
- Modify: `bench/agent/loop.py`
- Create: `apps/code_review_pipeline/delivery/adapters/bench_agent.py`
- Modify: `apps/code_review_pipeline/delivery/adapters/__init__.py`
- Modify: `tests/unit/test_bench_loop_session.py`
- Modify: `tests/unit/test_delivery_import_boundaries.py`
- Test: `tests/unit/test_delivery_bench_adapter.py`

**Interfaces:**
- Consumes: `LoopSession.replace_messages(messages)`, `bench.agent.loop.LLMBackedAgent`, `bench.agent.types.Budget`, `NO_COST_LIMIT`, `build_toolset`, `parse_verifier_output`.
- Produces: `LoopSession.replace_messages(messages: Sequence[Mapping[str, Any]]) -> None`, `BenchDeliveryImplementer`, `BenchDeliveryVerifier`.

- [ ] **Step 1: Write the failing public message-injection test**

Append this test to `tests/unit/test_bench_loop_session.py`.

```python
@pytest.mark.asyncio
async def test_session_messages_can_be_replaced_for_a_role_specific_prompt(
    tmp_path,
) -> None:
    session = _session(tmp_path, [ScriptedTurn(final="done")])
    replacement = [
        {"role": "system", "content": "read-only verifier"},
        {"role": "user", "content": "review bundle abc"},
    ]

    session.replace_messages(replacement)
    replacement[0]["content"] = "mutated after injection"

    assert await session.advance() == "completed"
    seen = session._runner._llm.seen[-1]
    assert seen[0] == {"role": "system", "content": "read-only verifier"}
    assert seen[1] == {"role": "user", "content": "review bundle abc"}
```

Run:

```powershell
uv run pytest tests/unit/test_bench_loop_session.py -q
```

Expected: FAIL because `LoopSession` has no `replace_messages` method.

- [ ] **Step 2: Add the public message-injection seam**

In `bench/agent/loop.py`, import `Mapping` and `Sequence` from
`collections.abc`. Add this method to `LoopSession` directly after
`add_user_message`.

```python
    def replace_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> None:
        """Replace the initial prompt for a role-specific, independent session.

        The bench Verifier needs a structured system+user prompt rather than the
        Implementer's default ``workspace/goal`` pair. Keeping this operation on
        ``LoopSession`` avoids experiment code reaching into ``_messages`` and
        preserves the invariant that message history is owned in one place.
        """
        clean = [dict(message) for message in messages]
        if not clean:
            raise ValueError("messages 不能为空")
        for index, message in enumerate(clean):
            role = message.get("role")
            content = message.get("content")
            if not isinstance(role, str) or not role.strip():
                raise ValueError(f"messages[{index}].role 必须是非空字符串")
            if not isinstance(content, str):
                raise ValueError(f"messages[{index}].content 必须是字符串")
        self._messages = clean
```

Run:

```powershell
uv run pytest tests/unit/test_bench_loop_session.py -q
uv run ruff check bench/agent/loop.py tests/unit/test_bench_loop_session.py
```

Expected: all existing continuation tests and the new replacement test pass.

- [ ] **Step 3: Extend the import-boundary test**

Add this test to `tests/unit/test_delivery_import_boundaries.py`.

```python
def test_only_the_bench_adapter_imports_bench() -> None:
    root = ROOT / "apps" / "code_review_pipeline" / "delivery"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "bench_agent.py":
            continue
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            1,
        ):
            if line.strip().startswith(("import bench", "from bench")):
                offenders.append(f"{path.relative_to(ROOT)}:{line_no}")
    assert offenders == []
```

- [ ] **Step 4: Write the failing bench adapter test**

Create `tests/unit/test_delivery_bench_adapter.py`.

```python
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.provider import ChatResult
from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.delivery.adapters.bench_agent import (
    BenchDeliveryVerifier,
)
from apps.code_review_pipeline.delivery.models import AgentAttempt, DeliveryUsage


def _bundle() -> ChangeBundle:
    return ChangeBundle(
        base_sha="a" * 40,
        branch="moa/fix-trace-1",
        title="fix",
        body="",
        files=(),
        patch="diff --git a/app.py b/app.py\n",
        patch_hash="p" * 64,
        verify_command=("pytest", "-q"),
        verify_exit_code=0,
        verify_output="1 passed",
        model="test-model",
        trace_id="trace-1",
    )


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class FakeBenchLLM:
    def __init__(self, final: str) -> None:
        self.config = SimpleNamespace(model="test-model", temperature=0.0)
        self.last_metrics = {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "cost_usd": 0.0,
            "llm_latency_ms": 2.0,
        }
        self.tool_names: list[list[str]] = []
        self.responses = [
            [_call("1", "read_file", {"path": "app.py"})],
            final,
        ]

    async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResult:
        self.tool_names.append([item["function"]["name"] for item in tools])
        response = self.responses.pop(0)
        if isinstance(response, str):
            return ChatResult(
                messages=[*messages, {"role": "assistant", "content": response}],
                content=response,
                tool_calls=[],
            )
        return ChatResult(
            messages=[*messages, {"role": "assistant", "content": None}],
            content="",
            tool_calls=response,
        )


@pytest.mark.asyncio
async def test_bench_verifier_maps_agent_run_and_uses_read_only_tools(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    final = json.dumps(
        {
            "decision": "approved",
            "reason": "verified",
            "instructions": "",
            "evidence": [
                {
                    "kind": "file",
                    "ref": "app.py",
                    "observation": "VALUE = 1",
                }
            ],
        }
    )
    llm = FakeBenchLLM(final)
    verifier = BenchDeliveryVerifier(
        llm=llm,
        verify_command=("python", "-c", "raise SystemExit(0)"),
    )

    verdict = await verifier.review(
        workspace=tmp_path,
        goal="verify",
        bundle=_bundle(),
        implementer_attempt=AgentAttempt(
            role="implementer",
            status="completed",
            summary="changed",
            stop_reason="finished",
            usage=DeliveryUsage(),
            steps=(),
        ),
        max_steps=5,
        max_tokens=1000,
    )

    assert verdict.decision == "approved"
    assert verdict.attempt.usage.prompt_tokens == 24
    assert set(llm.tool_names[0]) == {"read_file", "search", "run_tests"}
    assert "write_file" not in llm.tool_names[0]
```

- [ ] **Step 5: Run the tests to verify they fail**

Run:

```powershell
uv run pytest tests/unit/test_delivery_bench_adapter.py tests/unit/test_delivery_import_boundaries.py tests/unit/test_bench_loop_session.py -q
```

Expected: FAIL because `delivery.adapters.bench_agent` does not exist.

- [ ] **Step 6: Implement the bench adapters**

Create `apps/code_review_pipeline/delivery/adapters/bench_agent.py`.

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.workspace_tools import build_toolset
from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    AgentStep,
    DeliveryUsage,
)
from apps.code_review_pipeline.delivery.verification import (
    build_verifier_messages,
    parse_verifier_output,
)

from bench.agent.loop import LLMBackedAgent
from bench.agent.types import AgentRun, Budget
from bench.harness.budget import NO_COST_LIMIT

_IMPLEMENTER_INSTRUCTIONS = (
    "You are the Implementer. Modify only the workspace, use the fixed tests, "
    "and finish with a concise summary. You cannot push, approve, or merge."
)
_VERIFIER_INSTRUCTIONS = (
    "You are the read-only Verifier. Inspect the workspace and tests, then emit "
    "one strict JSON verdict. Never modify files and never claim approval."
)


def _attempt(run: AgentRun, role: str) -> AgentAttempt:
    return AgentAttempt(
        role=role,
        status=run.status,
        summary=(run.stop_reason or run.error or "")[:2000],
        stop_reason=run.stop_reason or run.error,
        usage=DeliveryUsage(
            llm_calls=run.usage.llm_calls,
            prompt_tokens=run.usage.prompt_tokens,
            completion_tokens=run.usage.completion_tokens,
            cost_usd=run.usage.cost_usd,
            llm_latency_ms=run.usage.llm_latency_ms,
        ),
        steps=tuple(
            AgentStep(
                index=step.index,
                action=step.action,
                tool=step.tool,
                arguments=dict(step.arguments),
                observation=step.observation,
                outcome=step.outcome,
                elapsed_ms=step.elapsed_ms,
            )
            for step in run.steps
        ),
    )


class BenchDeliveryImplementer:
    def __init__(self, *, llm: Any, verify_command: tuple[str, ...]) -> None:
        self._llm = llm
        self._verify_command = tuple(verify_command)

    async def run(
        self,
        *,
        workspace: Path,
        goal: str,
        feedback: str,
        max_steps: int,
        max_tokens: int,
    ) -> AgentAttempt:
        tools = build_toolset(
            workspace=workspace,
            verify_command=self._verify_command,
            names=("read_file", "write_file", "search", "run_tests"),
        )
        agent = LLMBackedAgent(
            self._llm,
            tools,
            instructions=_IMPLEMENTER_INSTRUCTIONS,
        )
        prompt = goal if not feedback.strip() else (
            f"{goal}\n\nRequired revision:\n{feedback.strip()}"
        )
        run = await agent.run(
            workspace=workspace,
            goal=prompt,
            budget=Budget(
                max_cost_usd=NO_COST_LIMIT,
                max_steps=max_steps,
                max_tokens=max_tokens,
            ),
        )
        return _attempt(run, "implementer")


class BenchDeliveryVerifier:
    def __init__(self, *, llm: Any, verify_command: tuple[str, ...]) -> None:
        self._llm = llm
        self._verify_command = tuple(verify_command)

    async def review(
        self,
        *,
        workspace: Path,
        goal: str,
        bundle: ChangeBundle,
        implementer_attempt: AgentAttempt,
        max_steps: int,
        max_tokens: int,
    ):
        tools = build_toolset(
            workspace=workspace,
            verify_command=self._verify_command,
            names=("read_file", "search", "run_tests"),
        )
        agent = LLMBackedAgent(
            self._llm,
            tools,
            instructions=_VERIFIER_INSTRUCTIONS,
        )
        session = agent.session(
            workspace=workspace,
            goal=goal,
            budget=Budget(
                max_cost_usd=NO_COST_LIMIT,
                max_steps=max_steps,
                max_tokens=max_tokens,
            ),
        )
        prompt = build_verifier_messages(
            workspace=str(Path(workspace).resolve()),
            goal=goal,
            bundle=bundle,
            implementer_attempt=implementer_attempt,
        )
        session.replace_messages(prompt)
        status = await session.advance(max_turns=max_steps)
        stop_reason = (
            "模型声明完成"
            if status == "completed"
            else f"Verifier stopped with status {status}"
        )
        run = session.build_run(status, stop_reason=stop_reason)
        attempt = _attempt(run, "verifier")
        return parse_verifier_output(
            session.finish_content,
            attempt=attempt,
            bundle=bundle,
        )
```

`replace_messages` is the only message-injection seam. The experiment adapter
must not access the session's private message field directly.

Update `adapters/__init__.py` to export both bench classes. That module is
experimental; production CLI code imports the concrete classes from their own
modules and never imports `bench_agent.py`.

- [ ] **Step 7: Run adapter and boundary tests**

Run:

```powershell
uv run pytest tests/unit/test_delivery_bench_adapter.py tests/unit/test_delivery_import_boundaries.py tests/unit/test_bench_loop_session.py -q
uv run ruff check bench/agent/loop.py apps/code_review_pipeline/delivery/adapters tests/unit/test_delivery_bench_adapter.py tests/unit/test_delivery_import_boundaries.py tests/unit/test_bench_loop_session.py
```

Expected: all pass; `write_file` is absent from every Verifier schema.

- [ ] **Step 8: Commit**

```powershell
git add bench/agent/loop.py apps/code_review_pipeline/delivery/adapters/bench_agent.py apps/code_review_pipeline/delivery/adapters/__init__.py tests/unit/test_bench_loop_session.py tests/unit/test_delivery_bench_adapter.py tests/unit/test_delivery_import_boundaries.py
git commit -m "test: add experiment-only verifier adapter"
```

---

### Task 5: Freeze the Experiment Contracts, Task Set, and Protocol

**Files:**
- Create: `bench/delivery/__init__.py`
- Create: `bench/delivery/models.py`
- Create: `bench/delivery/protocol.py`
- Create: `bench/delivery/protocols/repository-delivery-s0-s1-v1.json`
- Create: `bench/delivery/tasks/feishu-post-rich-text/task.json`
- Create: `bench/delivery/tasks/feishu-post-rich-text/visible/tests/delivery_visible/test_feishu_post.py`
- Create: `bench/delivery/tasks/feishu-post-rich-text/hidden/tests/delivery_hidden/test_feishu_post_hidden.py`
- Create: `bench/delivery/tasks/budget-ttl-refresh/task.json`
- Create: `bench/delivery/tasks/budget-ttl-refresh/visible/defect.patch`
- Create: `bench/delivery/tasks/budget-ttl-refresh/visible/tests/delivery_visible/test_budget_ttl.py`
- Create: `bench/delivery/tasks/budget-ttl-refresh/hidden/tests/delivery_hidden/test_budget_ttl_hidden.py`
- Create: `bench/delivery/tasks/keyword-normalization/task.json`
- Create: `bench/delivery/tasks/keyword-normalization/visible/defect.patch`
- Create: `bench/delivery/tasks/keyword-normalization/visible/tests/delivery_visible/test_keyword_normalization.py`
- Create: `bench/delivery/tasks/keyword-normalization/hidden/tests/delivery_hidden/test_keyword_normalization_hidden.py`
- Create: `bench/delivery/tasks/binary-write-review/task.json`
- Create: `bench/delivery/tasks/binary-write-review/visible/defect.patch`
- Create: `bench/delivery/tasks/binary-write-review/visible/tests/delivery_visible/test_binary_write_review.py`
- Create: `bench/delivery/tasks/binary-write-review/hidden/tests/delivery_hidden/test_binary_write_review_hidden.py`
- Test: `tests/unit/test_delivery_protocol.py`

**Interfaces:**
- Consumes: `DeliveryRun`, `DeliveryUsage`, repository-relative task fixtures.
- Produces: `DeliveryTaskDefinition`, `DeliveryTrial`, `DeliveryExperimentRecord`, `DeliveryProtocol`, `load_delivery_task(task_dir)`, `load_delivery_protocol(path, *, tasks_root=None)`, `compute_task_set_hash(tasks)`.

- [ ] **Step 1: Write the failing task/protocol tests**

Create `tests/unit/test_delivery_protocol.py`.

```python
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bench.delivery.protocol import (
    compute_task_set_hash,
    load_delivery_protocol,
    load_delivery_task,
)

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = (
    ROOT
    / "bench"
    / "delivery"
    / "protocols"
    / "repository-delivery-s0-s1-v1.json"
)
TASKS_ROOT = ROOT / "bench" / "delivery" / "tasks"


def test_the_protocol_freezes_the_real_task_set_and_model() -> None:
    protocol = load_delivery_protocol(PROTOCOL_PATH, tasks_root=TASKS_ROOT)

    assert protocol.protocol_id == "governed-repository-delivery-s0-s1"
    assert protocol.version == "v1"
    assert [task.task_id for task in protocol.tasks] == [
        "delivery-feishu-post-rich-text",
        "delivery-budget-ttl-refresh",
        "delivery-keyword-normalization",
        "delivery-binary-write-review",
    ]
    assert [task.base_commit for task in protocol.tasks] == [
        "988fccbb242be0573f4e42f05118276884272bf3",
        "910c865b0a75af1b745e3054555de5509c46a236",
        "910c865b0a75af1b745e3054555de5509c46a236",
        "910c865b0a75af1b745e3054555de5509c46a236",
    ]
    assert protocol.model.model == "nvidia/nemotron-3-super-120b-a12b"
    assert protocol.model.temperature == 1.0
    assert protocol.budget.max_steps == 24
    assert protocol.budget.max_tokens == 120_000
    assert [arm.name for arm in protocol.arms] == ["S0", "S1"]
    assert protocol.repeats.pilot == 2
    assert protocol.repeats.formal == 4
    assert len(protocol.task_set_hash) == 64
    assert len(protocol.config_hash) == 64


def test_task_set_hash_changes_when_a_hidden_test_changes(
    tmp_path: Path,
) -> None:
    copied = tmp_path / "tasks"
    shutil.copytree(TASKS_ROOT, copied)
    task_dir = copied / "feishu-post-rich-text"
    task = load_delivery_task(task_dir)
    before = compute_task_set_hash((task,))

    hidden = task_dir / "hidden" / task.hidden_tests[0]
    hidden.write_text(
        hidden.read_text(encoding="utf-8") + "\n# changed\n",
        encoding="utf-8",
    )
    after = compute_task_set_hash((load_delivery_task(task_dir),))

    assert before != after


def test_protocol_rejects_an_arm_outside_s0_s1(tmp_path: Path) -> None:
    payload = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    payload["arms"].append(
        {
            "name": "S2",
            "version": "v1",
            "description": "not part of this protocol",
        }
    )
    broken = tmp_path / "protocol.json"
    broken.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="S0/S1"):
        load_delivery_protocol(broken, tasks_root=TASKS_ROOT)


def test_protocol_cannot_reference_legacy_v5_or_v8_results() -> None:
    protocol = load_delivery_protocol(PROTOCOL_PATH, tasks_root=TASKS_ROOT)
    serialized = json.dumps(protocol.to_dict(), ensure_ascii=False)

    assert protocol.protocol_id not in protocol.exclusions.forbidden_protocol_ids
    assert "multi-agent-first-batch-v5-local" in protocol.exclusions.forbidden_protocol_ids
    assert "multi-agent-first-batch-v8-local" in protocol.exclusions.forbidden_protocol_ids
    assert '"legacy_results"' not in serialized
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run pytest tests/unit/test_delivery_protocol.py -q
```

Expected: FAIL because `bench.delivery.protocol` and the task fixtures do not
exist.

- [ ] **Step 3: Add the experiment data contracts**

Create `bench/delivery/__init__.py`.

```python
"""Pre-registered S0/S1 repository-delivery experiment."""

from bench.delivery.models import (
    DeliveryExperimentRecord,
    DeliveryTaskDefinition,
    DeliveryTrial,
)
from bench.delivery.protocol import (
    DeliveryProtocol,
    compute_task_set_hash,
    load_delivery_protocol,
    load_delivery_task,
)

__all__ = [
    "DeliveryExperimentRecord",
    "DeliveryProtocol",
    "DeliveryTaskDefinition",
    "DeliveryTrial",
    "compute_task_set_hash",
    "load_delivery_protocol",
    "load_delivery_task",
]
```

Create `bench/delivery/models.py`.

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apps.code_review_pipeline.delivery.models import DeliveryRun, DeliveryUsage

SCHEMA_VERSION = 1

DeliveryArm = Literal["S0", "S1"]
DeliveryPhase = Literal["pilot", "formal"]
DeliveryTrialStatus = Literal["valid", "error"]


@dataclass(frozen=True)
class DeliveryTaskDefinition:
    task_id: str
    base_commit: str
    source: Literal["E1", "E3"]
    goal: str
    verify_command: tuple[str, ...]
    hidden_command: tuple[str, ...]
    visible_tests: tuple[str, ...]
    hidden_tests: tuple[str, ...]
    protected_roots: tuple[str, ...]
    fail_first_marker: str
    hidden_fail_first_marker: str
    task_dir: Path
    defect_patch: Path | None

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id 不能为空")
        if len(self.base_commit) != 40 or any(
            char not in "0123456789abcdef" for char in self.base_commit
        ):
            raise ValueError(f"base_commit 必须是 40 位小写 SHA: {self.base_commit!r}")
        if self.source not in {"E1", "E3"}:
            raise ValueError(f"source 非法: {self.source!r}")
        if not self.goal.strip():
            raise ValueError("goal 不能为空")
        if not self.verify_command or not self.hidden_command:
            raise ValueError("visible/hidden command 都不能为空")
        if not self.visible_tests or not self.hidden_tests:
            raise ValueError("visible/hidden tests 都不能为空")
        if not self.protected_roots:
            raise ValueError("protected_roots 不能为空")
        protected = set(self.protected_roots)
        if any(
            not Path(item).parts
            or Path(item).parts[0] not in protected
            for item in (*self.visible_tests, *self.hidden_tests)
        ):
            raise ValueError("visible/hidden tests 必须位于 protected_roots 内")
        if not self.fail_first_marker.strip():
            raise ValueError("fail_first_marker 不能为空")
        if not self.hidden_fail_first_marker.strip():
            raise ValueError("hidden_fail_first_marker 不能为空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "base_commit": self.base_commit,
            "source": self.source,
            "goal": self.goal,
            "verify_command": list(self.verify_command),
            "hidden_command": list(self.hidden_command),
            "visible_tests": list(self.visible_tests),
            "hidden_tests": list(self.hidden_tests),
            "protected_roots": list(self.protected_roots),
            "fail_first_marker": self.fail_first_marker,
            "hidden_fail_first_marker": self.hidden_fail_first_marker,
            "defect_patch": (
                self.defect_patch.relative_to(self.task_dir).as_posix()
                if self.defect_patch is not None
                else None
            ),
        }


@dataclass(frozen=True)
class DeliveryTrial:
    schema_version: int
    trial_id: str
    protocol_hash: str
    phase: DeliveryPhase
    task_id: str
    arm: DeliveryArm
    repeat: int
    model: str
    status: DeliveryTrialStatus
    delivery_run: DeliveryRun | None
    usage: DeliveryUsage
    visible_passed: bool
    hidden_passed: bool
    error_release: bool
    revise_attempted: bool
    revise_recovered: bool
    verifier_invalid: bool
    patch: str
    visible_output: str
    hidden_output: str
    error: str
    wall_clock_s: float

    @property
    def failure_code(self) -> str:
        if self.delivery_run is None or self.delivery_run.failure is None:
            return ""
        return self.delivery_run.failure.code.value

    @property
    def run_state(self) -> str:
        if self.delivery_run is None:
            return ""
        return self.delivery_run.state.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trial_id": self.trial_id,
            "protocol_hash": self.protocol_hash,
            "phase": self.phase,
            "task_id": self.task_id,
            "arm": self.arm,
            "repeat": self.repeat,
            "model": self.model,
            "status": self.status,
            "delivery_run": (
                self.delivery_run.to_dict()
                if self.delivery_run is not None
                else None
            ),
            "usage": self.usage.to_dict(),
            "visible_passed": self.visible_passed,
            "hidden_passed": self.hidden_passed,
            "error_release": self.error_release,
            "revise_attempted": self.revise_attempted,
            "revise_recovered": self.revise_recovered,
            "verifier_invalid": self.verifier_invalid,
            "failure_code": self.failure_code,
            "run_state": self.run_state,
            "patch": self.patch,
            "visible_output": self.visible_output,
            "hidden_output": self.hidden_output,
            "error": self.error,
            "wall_clock_s": self.wall_clock_s,
        }


@dataclass(frozen=True)
class DeliveryExperimentRecord:
    schema_version: int
    protocol_id: str
    protocol_hash: str
    phase: DeliveryPhase
    started_at: str
    trials: tuple[DeliveryTrial, ...]

    def __post_init__(self) -> None:
        ids = [trial.trial_id for trial in self.trials]
        if len(ids) != len(set(ids)):
            raise ValueError("DeliveryExperimentRecord 包含重复 trial_id")
        if any(trial.protocol_hash != self.protocol_hash for trial in self.trials):
            raise ValueError("同一记录的 trial protocol_hash 必须一致")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "protocol_id": self.protocol_id,
            "protocol_hash": self.protocol_hash,
            "phase": self.phase,
            "started_at": self.started_at,
            "trials": [trial.to_dict() for trial in self.trials],
        }
```

- [ ] **Step 4: Implement the frozen protocol loader and task hashing**

Create `bench/delivery/protocol.py`.

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from bench.delivery.models import DeliveryTaskDefinition

PROTOCOL_SCHEMA_VERSION = 1


def _mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise TypeError(f"protocol.{key} 必须是对象")
    return value


def _sequence(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise TypeError(f"protocol.{key} 必须是数组")
    return value


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"protocol.{key} 必须是非空字符串")
    return value.strip()


def _text_item(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{label} 必须是非空字符串")
    return value.strip()


def _integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"protocol.{key} 必须是整数")
    return value


def _number(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"protocol.{key} 必须是数字")
    return float(value)


def _boolean(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise TypeError(f"protocol.{key} 必须是布尔值")
    return value


def _safe_child(root: Path, raw: str) -> Path:
    candidate = (root / raw).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"task 路径越界: {raw!r}")
    return candidate


def _string_tuple(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    return tuple(
        _text_item(item, f"{key}[{index}]")
        for index, item in enumerate(_sequence(payload, key))
    )


def load_delivery_task(task_dir: Path) -> DeliveryTaskDefinition:
    root = Path(task_dir).resolve()
    payload = json.loads((root / "task.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("task.json 必须是对象")
    visible_tests = _string_tuple(payload, "visible_tests")
    hidden_tests = _string_tuple(payload, "hidden_tests")
    for relative in visible_tests:
        if not _safe_child(root / "visible", relative).is_file():
            raise FileNotFoundError(f"visible test 不存在: {relative}")
    for relative in hidden_tests:
        if not _safe_child(root / "hidden", relative).is_file():
            raise FileNotFoundError(f"hidden test 不存在: {relative}")
    defect_raw = payload.get("defect_patch")
    defect_patch = None
    if defect_raw is not None:
        if not isinstance(defect_raw, str) or not defect_raw.strip():
            raise TypeError("defect_patch 必须是非空字符串或 null")
        defect_patch = _safe_child(root, defect_raw)
        if not defect_patch.is_file():
            raise FileNotFoundError(f"defect_patch 不存在: {defect_raw}")
    return DeliveryTaskDefinition(
        task_id=_text(payload, "task_id"),
        base_commit=_text(payload, "base_commit"),
        source=_text(payload, "source"),
        goal=_text(payload, "goal"),
        verify_command=_string_tuple(payload, "verify_command"),
        hidden_command=_string_tuple(payload, "hidden_command"),
        visible_tests=visible_tests,
        hidden_tests=hidden_tests,
        protected_roots=_string_tuple(payload, "protected_roots"),
        fail_first_marker=_text(payload, "fail_first_marker"),
        hidden_fail_first_marker=_text(payload, "hidden_fail_first_marker"),
        task_dir=root,
        defect_patch=defect_patch,
    )


def compute_task_set_hash(
    tasks: Sequence[DeliveryTaskDefinition],
) -> str:
    digest = hashlib.sha256()
    for task in sorted(tasks, key=lambda item: item.task_id):
        digest.update(task.task_id.encode("utf-8"))
        digest.update(b"\0")
        for path in sorted(task.task_dir.rglob("*")):
            if not path.is_file():
                continue
            digest.update(path.relative_to(task.task_dir).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class DeliveryProtocolModel:
    provider: str
    model: str
    temperature: float
    max_output_tokens: int

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeliveryProtocolModel":
        result = cls(
            provider=_text(payload, "provider"),
            model=_text(payload, "model"),
            temperature=_number(payload, "temperature"),
            max_output_tokens=_integer(payload, "max_output_tokens"),
        )
        if result.max_output_tokens <= 0:
            raise ValueError("protocol.model.max_output_tokens 必须 > 0")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True)
class DeliveryProtocolBudget:
    max_steps: int
    max_tokens: int

    def __post_init__(self) -> None:
        if self.max_steps <= 0 or self.max_tokens <= 0:
            raise ValueError("protocol.budget 必须全部 > 0")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeliveryProtocolBudget":
        return cls(
            max_steps=_integer(payload, "max_steps"),
            max_tokens=_integer(payload, "max_tokens"),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_steps": self.max_steps,
            "max_tokens": self.max_tokens,
        }


@dataclass(frozen=True)
class DeliveryProtocolArm:
    name: Literal["S0", "S1"]
    version: str
    description: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeliveryProtocolArm":
        name = _text(payload, "name")
        if name not in {"S0", "S1"}:
            raise ValueError("protocol arms 只允许 S0/S1")
        return cls(
            name=name,
            version=_text(payload, "version"),
            description=_text(payload, "description"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
        }


@dataclass(frozen=True)
class DeliveryProtocolRepeats:
    pilot: int
    formal: int

    def __post_init__(self) -> None:
        if self.pilot <= 0 or self.formal <= 0:
            raise ValueError("protocol.repeats 必须 > 0")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeliveryProtocolRepeats":
        return cls(
            pilot=_integer(payload, "pilot"),
            formal=_integer(payload, "formal"),
        )

    def for_phase(self, phase: str) -> int:
        if phase == "pilot":
            return self.pilot
        if phase == "formal":
            return self.formal
        raise ValueError(f"未知 phase: {phase!r}")

    def to_dict(self) -> dict[str, int]:
        return {"pilot": self.pilot, "formal": self.formal}


@dataclass(frozen=True)
class DeliveryProtocolGates:
    pilot_baseline_min_exclusive: float
    pilot_baseline_max_exclusive: float
    max_unattributed_errors: int
    max_hidden_pass_drop: float
    require_all_cells: bool
    require_error_release_improvement: bool

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeliveryProtocolGates":
        result = cls(
            pilot_baseline_min_exclusive=_number(
                payload, "pilot_baseline_min_exclusive"
            ),
            pilot_baseline_max_exclusive=_number(
                payload, "pilot_baseline_max_exclusive"
            ),
            max_unattributed_errors=_integer(
                payload, "max_unattributed_errors"
            ),
            max_hidden_pass_drop=_number(payload, "max_hidden_pass_drop"),
            require_all_cells=_boolean(payload, "require_all_cells"),
            require_error_release_improvement=_boolean(
                payload, "require_error_release_improvement"
            ),
        )
        if not (
            result.pilot_baseline_min_exclusive
            < result.pilot_baseline_max_exclusive
        ):
            raise ValueError("pilot baseline 下界必须小于上界")
        if result.max_unattributed_errors < 0:
            raise ValueError("max_unattributed_errors 不能为负")
        if not 0 <= result.max_hidden_pass_drop <= 1:
            raise ValueError("max_hidden_pass_drop 必须在 [0, 1]")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "pilot_baseline_min_exclusive": (
                self.pilot_baseline_min_exclusive
            ),
            "pilot_baseline_max_exclusive": (
                self.pilot_baseline_max_exclusive
            ),
            "max_unattributed_errors": self.max_unattributed_errors,
            "max_hidden_pass_drop": self.max_hidden_pass_drop,
            "require_all_cells": self.require_all_cells,
            "require_error_release_improvement": (
                self.require_error_release_improvement
            ),
        }


@dataclass(frozen=True)
class DeliveryProtocolExclusions:
    forbidden_protocol_ids: tuple[str, ...]
    forbidden_result_patterns: tuple[str, ...]

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
    ) -> "DeliveryProtocolExclusions":
        return cls(
            forbidden_protocol_ids=_string_tuple(payload, "forbidden_protocol_ids"),
            forbidden_result_patterns=_string_tuple(
                payload, "forbidden_result_patterns"
            ),
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "forbidden_protocol_ids": list(self.forbidden_protocol_ids),
            "forbidden_result_patterns": list(self.forbidden_result_patterns),
        }


@dataclass(frozen=True)
class DeliveryProtocol:
    schema_version: int
    protocol_id: str
    version: str
    frozen_at: str
    tasks: tuple[DeliveryTaskDefinition, ...]
    task_set_hash: str
    model: DeliveryProtocolModel
    budget: DeliveryProtocolBudget
    arms: tuple[DeliveryProtocolArm, ...]
    repeats: DeliveryProtocolRepeats
    min_valid_repeats_per_cell: int
    gates: DeliveryProtocolGates
    exclusions: DeliveryProtocolExclusions

    def __post_init__(self) -> None:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("protocol tasks 包含重复 task_id")
        if [arm.name for arm in self.arms] != ["S0", "S1"]:
            raise ValueError("protocol arms 必须恰好是按序排列的 S0/S1")
        if self.min_valid_repeats_per_cell <= 0:
            raise ValueError("min_valid_repeats_per_cell 必须 > 0")
        if self.min_valid_repeats_per_cell > min(
            self.repeats.pilot,
            self.repeats.formal,
        ):
            raise ValueError("min_valid_repeats_per_cell 超过某阶段重复数")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "protocol_id": self.protocol_id,
            "version": self.version,
            "frozen_at": self.frozen_at,
            "tasks": [task.to_dict() for task in self.tasks],
            "task_set_hash": self.task_set_hash,
            "model": self.model.to_dict(),
            "budget": self.budget.to_dict(),
            "arms": [arm.to_dict() for arm in self.arms],
            "repeats": self.repeats.to_dict(),
            "min_valid_repeats_per_cell": self.min_valid_repeats_per_cell,
            "gates": self.gates.to_dict(),
            "exclusions": self.exclusions.to_dict(),
        }

    @property
    def config_hash(self) -> str:
        raw = json.dumps(
            self.identity_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "protocol_hash": self.config_hash}


def load_delivery_protocol(
    path: Path,
    *,
    tasks_root: Path | None = None,
) -> DeliveryProtocol:
    source = Path(path).resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("protocol 根节点必须是对象")
    schema_version = _integer(payload, "schema_version")
    if schema_version != PROTOCOL_SCHEMA_VERSION:
        raise ValueError(
            f"不支持的 protocol schema_version: {schema_version}"
            f"（只认 {PROTOCOL_SCHEMA_VERSION}）"
        )
    default_root = (
        source.parents[3]
        if len(source.parents) > 3
        else Path.cwd()
    )
    raw_root = tasks_root or default_root / _text(payload, "tasks_root")
    root = Path(raw_root).resolve()
    task_dirs = _string_tuple(payload, "task_dirs")
    tasks = tuple(
        load_delivery_task(_safe_child(root, relative))
        for relative in task_dirs
    )
    arms = tuple(
        DeliveryProtocolArm.from_dict(item)
        for item in (
            value
            for value in _sequence(payload, "arms")
            if isinstance(value, dict)
        )
    )
    return DeliveryProtocol(
        schema_version=schema_version,
        protocol_id=_text(payload, "protocol_id"),
        version=_text(payload, "version"),
        frozen_at=_text(payload, "frozen_at"),
        tasks=tasks,
        task_set_hash=compute_task_set_hash(tasks),
        model=DeliveryProtocolModel.from_dict(_mapping(payload, "model")),
        budget=DeliveryProtocolBudget.from_dict(_mapping(payload, "budget")),
        arms=arms,
        repeats=DeliveryProtocolRepeats.from_dict(_mapping(payload, "repeats")),
        min_valid_repeats_per_cell=_integer(
            payload, "min_valid_repeats_per_cell"
        ),
        gates=DeliveryProtocolGates.from_dict(_mapping(payload, "gates")),
        exclusions=DeliveryProtocolExclusions.from_dict(
            _mapping(payload, "exclusions")
        ),
    )


__all__ = [
    "DeliveryProtocol",
    "DeliveryProtocolArm",
    "DeliveryProtocolBudget",
    "DeliveryProtocolExclusions",
    "DeliveryProtocolGates",
    "DeliveryProtocolModel",
    "DeliveryProtocolRepeats",
    "PROTOCOL_SCHEMA_VERSION",
    "compute_task_set_hash",
    "load_delivery_protocol",
    "load_delivery_task",
]
```

- [ ] **Step 5: Add the Feishu fail-first task**

Create `bench/delivery/tasks/feishu-post-rich-text/task.json`.

```json
{
  "task_id": "delivery-feishu-post-rich-text",
  "base_commit": "988fccbb242be0573f4e42f05118276884272bf3",
  "source": "E1",
  "goal": "Make parse_feishu_event extract text from Feishu rich-text post messages without duplicating content/content_v2, while preserving plain-text and unknown-message behavior.",
  "verify_command": [
    "{python}",
    "-m",
    "pytest",
    "tests/delivery_visible/test_feishu_post.py",
    "-q"
  ],
  "hidden_command": [
    "{python}",
    "-m",
    "pytest",
    "tests/delivery_hidden/test_feishu_post_hidden.py",
    "-q"
  ],
  "visible_tests": [
    "tests/delivery_visible/test_feishu_post.py"
  ],
  "hidden_tests": [
    "tests/delivery_hidden/test_feishu_post_hidden.py"
  ],
  "protected_roots": [
    "tests"
  ],
  "fail_first_marker": "assert None",
  "hidden_fail_first_marker": "assert None",
  "defect_patch": null
}
```

Create `bench/delivery/tasks/feishu-post-rich-text/visible/tests/delivery_visible/test_feishu_post.py`.

```python
from __future__ import annotations

import json

from app.channels.feishu_event import parse_feishu_event


def _post_event(text: str) -> dict:
    content = json.dumps(
        {
            "title": "",
            "content": [[{"tag": "text", "text": text, "style": []}]],
            "content_v2": [[{"tag": "text", "text": text, "style": []}]],
        },
        ensure_ascii=False,
    )
    return {
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": "delivery-feishu-visible",
        },
        "event": {
            "message": {
                "chat_id": "oc_delivery",
                "message_type": "post",
                "content": content,
            }
        },
    }


def test_post_message_text_is_extracted() -> None:
    parsed = parse_feishu_event(
        _post_event("记一下 本方案报价 1999 元/月 包含三次上门服务")
    )

    assert parsed["text"] == "记一下 本方案报价 1999 元/月 包含三次上门服务"
    assert parsed["chat_id"] == "oc_delivery"
```

Create `bench/delivery/tasks/feishu-post-rich-text/hidden/tests/delivery_hidden/test_feishu_post_hidden.py`.

```python
from __future__ import annotations

import json

from app.channels.feishu_event import parse_feishu_event


def _event(message_type: str, content: dict | str) -> dict:
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "chat_id": "oc_hidden",
                "message_type": message_type,
                "content": (
                    json.dumps(content, ensure_ascii=False)
                    if isinstance(content, dict)
                    else content
                ),
            }
        },
    }


def test_content_and_content_v2_are_not_concatenated() -> None:
    parsed = parse_feishu_event(
        _event(
            "post",
            {
                "content": [[{"tag": "text", "text": "alpha 1999"}]],
                "content_v2": [[{"tag": "text", "text": "alpha 1999"}]],
            },
        )
    )

    assert parsed["text"] == "alpha 1999"


def test_plain_text_message_still_works() -> None:
    assert parse_feishu_event(_event("text", {"text": "hello"}))["text"] == "hello"


def test_unknown_message_type_yields_empty_text() -> None:
    assert parse_feishu_event(_event("image", {"image_key": "k"}))["text"] == ""
```

- [ ] **Step 6: Add the budget TTL fail-first task**

Create `bench/delivery/tasks/budget-ttl-refresh/task.json`.

```json
{
  "task_id": "delivery-budget-ttl-refresh",
  "base_commit": "910c865b0a75af1b745e3054555de5509c46a236",
  "source": "E3",
  "goal": "Keep BudgetGuard session spend alive while the session is still recording cost, while preserving TTL eviction after a genuinely idle period.",
  "verify_command": [
    "{python}",
    "-m",
    "pytest",
    "tests/delivery_visible/test_budget_ttl.py",
    "-q"
  ],
  "hidden_command": [
    "{python}",
    "-m",
    "pytest",
    "tests/delivery_hidden/test_budget_ttl_hidden.py",
    "-q"
  ],
  "visible_tests": ["tests/delivery_visible/test_budget_ttl.py"],
  "hidden_tests": ["tests/delivery_hidden/test_budget_ttl_hidden.py"],
  "protected_roots": ["tests"],
  "fail_first_marker": "assert 0.0 == 0.6",
  "hidden_fail_first_marker": "assert 0.0 == 0.5",
  "defect_patch": "visible/defect.patch"
}
```

Create `bench/delivery/tasks/budget-ttl-refresh/visible/defect.patch`.

```diff
diff --git a/app/budget/guard.py b/app/budget/guard.py
--- a/app/budget/guard.py
+++ b/app/budget/guard.py
@@ -50,7 +50,6 @@ class BudgetGuard:
             return self.spent(session_id)
         entry = self._spend.setdefault(session_id, _SessionSpend())
         entry.total_usd += float(cost_usd)
-        entry.last_seen = time.monotonic()
         return entry.total_usd

     def spent(self, session_id: str) -> float:
```

Create `bench/delivery/tasks/budget-ttl-refresh/visible/tests/delivery_visible/test_budget_ttl.py`.

```python
from __future__ import annotations

import pytest

from app.budget.guard import BudgetGuard


def test_accumulated_cost_survives_ongoing_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "app.budget.guard.time.monotonic",
        lambda: now[0],
    )
    guard = BudgetGuard(limit_usd=10.0, ttl_seconds=0.05)

    guard.record("s1", 0.3)
    guard._spend["s1"].last_seen = now[0]
    now[0] = 100.03
    guard.record("s1", 0.3)
    now[0] = 100.06

    assert guard.check("s1") is True
    assert guard.spent("s1") == 0.6
```

Create `bench/delivery/tasks/budget-ttl-refresh/hidden/tests/delivery_hidden/test_budget_ttl_hidden.py`.

```python
from __future__ import annotations

import pytest

from app.budget.guard import BudgetGuard


def test_many_small_charges_keep_refreshing_the_same_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [200.0]
    monkeypatch.setattr(
        "app.budget.guard.time.monotonic",
        lambda: now[0],
    )
    guard = BudgetGuard(limit_usd=10.0, ttl_seconds=0.05)

    for offset in (0.0, 0.02, 0.04, 0.06, 0.08):
        now[0] = 200.0 + offset
        guard.record("s1", 0.1)
        if offset == 0.0:
            guard._spend["s1"].last_seen = now[0]
    now[0] = 200.09

    assert guard.check("s1") is True
    assert guard.spent("s1") == 0.5


def test_ttl_still_expires_after_a_genuinely_idle_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [300.0]
    monkeypatch.setattr(
        "app.budget.guard.time.monotonic",
        lambda: now[0],
    )
    guard = BudgetGuard(limit_usd=10.0, ttl_seconds=0.05)

    guard.record("s1", 0.3)
    guard._spend["s1"].last_seen = now[0]
    now[0] = 300.06

    assert guard.check("s1") is True
    assert guard.spent("s1") == 0.0
```

- [ ] **Step 7: Add the keyword-normalization fail-first task**

Create `bench/delivery/tasks/keyword-normalization/task.json`.

```json
{
  "task_id": "delivery-keyword-normalization",
  "base_commit": "910c865b0a75af1b745e3054555de5509c46a236",
  "source": "E3",
  "goal": "Normalize keyword scores against the candidate-pool best so the keyword signal stays inside 0..1 and remains comparable with cosine similarity.",
  "verify_command": [
    "{python}",
    "-m",
    "pytest",
    "tests/delivery_visible/test_keyword_normalization.py",
    "-q"
  ],
  "hidden_command": [
    "{python}",
    "-m",
    "pytest",
    "tests/delivery_hidden/test_keyword_normalization_hidden.py",
    "-q"
  ],
  "visible_tests": ["tests/delivery_visible/test_keyword_normalization.py"],
  "hidden_tests": ["tests/delivery_hidden/test_keyword_normalization_hidden.py"],
  "protected_roots": ["tests"],
  "fail_first_marker": "assert 3.0 == 1.0",
  "hidden_fail_first_marker": "assert 3.0 == 1.0",
  "defect_patch": "visible/defect.patch"
}
```

Create `bench/delivery/tasks/keyword-normalization/visible/defect.patch`.

```diff
diff --git a/app/vectordb/keywords.py b/app/vectordb/keywords.py
--- a/app/vectordb/keywords.py
+++ b/app/vectordb/keywords.py
@@ -115,8 +115,8 @@ def normalized_keyword_score(content: str, query_lower: str, pool_best: float) -
     or the documents happen to be.
     """
     if pool_best <= 0:
-        return 0.0
-    return min(keyword_score(content, query_lower) / pool_best, 1.0)
+        pool_best = 1.0
+    return keyword_score(content, query_lower)


 def match_metadata(doc_meta: dict, filter_meta: dict) -> bool:
```

Create `bench/delivery/tasks/keyword-normalization/visible/tests/delivery_visible/test_keyword_normalization.py`.

```python
from __future__ import annotations

from app.vectordb.keywords import normalized_keyword_score


def test_keyword_scores_are_comparable_across_the_pool() -> None:
    best = normalized_keyword_score("redis redis redis", "redis", 3)
    weaker = normalized_keyword_score("redis", "redis", 3)

    assert best == 1.0
    assert weaker < best
```

Create `bench/delivery/tasks/keyword-normalization/hidden/tests/delivery_hidden/test_keyword_normalization_hidden.py`.

```python
from __future__ import annotations

from app.vectordb.keywords import normalized_keyword_score


def test_non_positive_pool_best_returns_zero() -> None:
    assert normalized_keyword_score("redis redis", "redis", 0) == 0.0


def test_normalization_is_capped_and_idempotent() -> None:
    first = normalized_keyword_score("redis redis redis", "redis", 2)
    second = normalized_keyword_score("redis redis redis", "redis", 2)

    assert first == 1.0
    assert second == first


def test_a_weaker_match_keeps_a_relative_order() -> None:
    strong = normalized_keyword_score("redis redis redis", "redis", 4)
    medium = normalized_keyword_score("redis redis", "redis", 4)
    weak = normalized_keyword_score("redis", "redis", 4)

    assert strong > medium > weak
```

- [ ] **Step 8: Add the binary-write review fail-first task**

Create `bench/delivery/tasks/binary-write-review/task.json`.

```json
{
  "task_id": "delivery-binary-write-review",
  "base_commit": "910c865b0a75af1b745e3054555de5509c46a236",
  "source": "E3",
  "goal": "Treat binary write modes wb/ab/xb as write operations that require human review, without changing the existing text-mode behavior.",
  "verify_command": [
    "{python}",
    "-m",
    "pytest",
    "tests/delivery_visible/test_binary_write_review.py",
    "-q"
  ],
  "hidden_command": [
    "{python}",
    "-m",
    "pytest",
    "tests/delivery_hidden/test_binary_write_review_hidden.py",
    "-q"
  ],
  "visible_tests": ["tests/delivery_visible/test_binary_write_review.py"],
  "hidden_tests": ["tests/delivery_hidden/test_binary_write_review_hidden.py"],
  "protected_roots": ["tests"],
  "fail_first_marker": "assert False is True",
  "hidden_fail_first_marker": "assert False is True",
  "defect_patch": "visible/defect.patch"
}
```

Create `bench/delivery/tasks/binary-write-review/visible/defect.patch`.

```diff
diff --git a/app/evaluator/evaluator.py b/app/evaluator/evaluator.py
--- a/app/evaluator/evaluator.py
+++ b/app/evaluator/evaluator.py
@@ -34,7 +34,7 @@ _DANGEROUS_METHODS: set[str] = {
     "rmtree", "remove", "unlink", "chmod", "chown",
 }

-_WRITE_MODES: set[str] = {"w", "wb", "a", "ab", "x", "xb"}
+_WRITE_MODES: set[str] = {"w", "a", "x"}


 def _extract_call_names(node: ast.Call) -> list[str]:
```

Create `bench/delivery/tasks/binary-write-review/visible/tests/delivery_visible/test_binary_write_review.py`.

```python
from __future__ import annotations

import pytest

from app.evaluator.evaluator import RuleEvaluator


@pytest.mark.asyncio
async def test_binary_write_mode_needs_review() -> None:
    result = await RuleEvaluator().score("open('data.bin', 'wb')", intent="coding")

    assert result.need_human_review is True
    assert result.score == 0.0
```

Create `bench/delivery/tasks/binary-write-review/hidden/tests/delivery_hidden/test_binary_write_review_hidden.py`.

```python
from __future__ import annotations

import pytest

from app.evaluator.evaluator import RuleEvaluator


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["wb", "ab", "xb"])
async def test_all_binary_write_modes_need_review(mode: str) -> None:
    result = await RuleEvaluator().score(
        f"open('data.bin', '{mode}')",
        intent="coding",
    )

    assert result.need_human_review is True
    assert result.score == 0.0


@pytest.mark.asyncio
async def test_read_mode_does_not_trigger_write_review() -> None:
    result = await RuleEvaluator().score(
        "open('data.bin', 'rb')",
        intent="coding",
    )

    assert result.need_human_review is False
```

- [ ] **Step 9: Freeze the protocol JSON**

Create `bench/delivery/protocols/repository-delivery-s0-s1-v1.json`.

```json
{
  "schema_version": 1,
  "protocol_id": "governed-repository-delivery-s0-s1",
  "version": "v1",
  "frozen_at": "2026-10-27T00:00:00+08:00",
  "tasks_root": "bench/delivery/tasks",
  "task_dirs": [
    "feishu-post-rich-text",
    "budget-ttl-refresh",
    "keyword-normalization",
    "binary-write-review"
  ],
  "model": {
    "provider": "nvidia",
    "model": "nvidia/nemotron-3-super-120b-a12b",
    "temperature": 1.0,
    "max_output_tokens": 4096
  },
  "budget": {
    "max_steps": 24,
    "max_tokens": 120000
  },
  "arms": [
    {
      "name": "S0",
      "version": "s0-v1",
      "description": "One Implementer and the existing deterministic delivery chain."
    },
    {
      "name": "S1",
      "version": "s1-v1",
      "description": "The same Implementer plus one read-only Verifier and at most one revision."
    }
  ],
  "repeats": {
    "pilot": 2,
    "formal": 4
  },
  "min_valid_repeats_per_cell": 2,
  "gates": {
    "pilot_baseline_min_exclusive": 0.0,
    "pilot_baseline_max_exclusive": 1.0,
    "max_unattributed_errors": 0,
    "max_hidden_pass_drop": 0.1,
    "require_all_cells": true,
    "require_error_release_improvement": true
  },
  "exclusions": {
    "forbidden_protocol_ids": [
      "multi-agent-first-batch-v5-local",
      "multi-agent-first-batch-v8-local"
    ],
    "forbidden_result_patterns": [
      "*.v5.result.json",
      "*.v8.result.json"
    ]
  }
}
```

- [ ] **Step 10: Run the protocol and fixture tests**

Run:

```powershell
uv run pytest tests/unit/test_delivery_protocol.py -q
uv run ruff check bench/delivery tests/unit/test_delivery_protocol.py
```

Expected: all tests pass. The task-set hash is derived from the exact bytes of
all four task directories; no v5/v8 artifact is read.

- [ ] **Step 11: Commit**

```powershell
git add bench/delivery/__init__.py bench/delivery/models.py bench/delivery/protocol.py bench/delivery/protocols/repository-delivery-s0-s1-v1.json bench/delivery/tasks tests/unit/test_delivery_protocol.py
git commit -m "feat: freeze governed delivery experiment protocol"
```

---

### Task 6: Run S0/S1 Trials and Judge Hidden Tests

**Files:**
- Create: `bench/delivery/runner.py`
- Test: `tests/unit/test_delivery_experiment.py`

**Interfaces:**
- Consumes: `DeliveryTaskDefinition`, `DeliveryProtocol`, `DeliveryFlow`, `BenchDeliveryImplementer`, `BenchDeliveryVerifier`, `bench.harness.workspace.materialize`, `build_change_bundle`.
- Produces: `DeliveryTaskValidation`, `InvalidDeliveryTask`, `validate_delivery_task(...)`, `run_delivery_trial(...)`, `run_delivery_experiment(...)`.

- [ ] **Step 1: Write the failing runner tests**

Create `tests/unit/test_delivery_experiment.py`.

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    DeliveryRunState,
    DeliveryUsage,
)
from bench.delivery.protocol import load_delivery_task
from bench.delivery.runner import (
    InvalidDeliveryTask,
    _source_patch,
    run_delivery_trial,
    validate_delivery_task,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "delivery@test")
    _git(root, "config", "user.name", "delivery")
    (root / "value.txt").write_text("1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD").strip()


def _task(
    tmp_path: Path,
    base_commit: str,
    *,
    hidden_release_probe: bool = False,
) -> Path:
    task_dir = tmp_path / "tasks" / "value-fix"
    visible = task_dir / "visible" / "tests" / "delivery_visible" / "test_value.py"
    hidden = task_dir / "hidden" / "tests" / "delivery_hidden" / "test_hidden.py"
    visible.parent.mkdir(parents=True)
    hidden.parent.mkdir(parents=True)
    visible.write_text("# copied into the agent workspace\n", encoding="utf-8")
    hidden.write_text("# copied only into the hidden judge workspace\n", encoding="utf-8")
    hidden_check = (
        "from pathlib import Path; "
        "assert Path('value.txt').read_text().strip() == '2'; "
        "assert Path('never-created.txt').exists()"
        if hidden_release_probe
        else (
            "from pathlib import Path; "
            "assert Path('value.txt').read_text().strip() == '2'"
        )
    )
    payload = {
        "task_id": "value-fix",
        "base_commit": base_commit,
        "source": "E1",
        "goal": "change value.txt from 1 to 2",
        "verify_command": [
            "{python}",
            "-c",
            "from pathlib import Path; assert Path('value.txt').read_text().strip() == '2'",
        ],
        "hidden_command": ["{python}", "-c", hidden_check],
        "visible_tests": ["tests/delivery_visible/test_value.py"],
        "hidden_tests": ["tests/delivery_hidden/test_hidden.py"],
        "protected_roots": ["tests"],
        "fail_first_marker": "AssertionError",
        "hidden_fail_first_marker": "AssertionError",
        "defect_patch": None,
    }
    (task_dir / "task.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    return task_dir


class _Writer:
    def __init__(self, *, tamper_with_visible_test: bool = False) -> None:
        self.tamper_with_visible_test = tamper_with_visible_test

    async def run(self, *, workspace: Path, **_kwargs) -> AgentAttempt:
        (workspace / "value.txt").write_text("2\n", encoding="utf-8")
        if self.tamper_with_visible_test:
            target = workspace / "tests" / "delivery_visible" / "test_value.py"
            target.write_text("# model changed the visible test\n", encoding="utf-8")
        return AgentAttempt(
            role="implementer",
            status="completed",
            summary="changed value",
            stop_reason="done",
            usage=DeliveryUsage(llm_calls=1, prompt_tokens=10, completion_tokens=2),
            steps=(),
        )


async def _no_verifier(_llm, _command):
    raise AssertionError("S0 must not construct a Verifier")


@pytest.mark.asyncio
async def test_a_visible_pass_is_judged_by_hidden_tests(tmp_path: Path) -> None:
    repo, commit = _repo(tmp_path)
    task = load_delivery_task(_task(tmp_path, commit))

    trial = await run_delivery_trial(
        task,
        repo=repo,
        workdir=tmp_path / "work",
        arm="S0",
        repeat=1,
        phase="pilot",
        protocol_hash="a" * 64,
        model="test-model",
        max_steps=8,
        max_tokens=10_000,
        llm=None,
        implementer_factory=lambda _llm, _command: _Writer(
            tamper_with_visible_test=True
        ),
        verifier_factory=_no_verifier,
    )

    assert trial.status == "valid"
    assert trial.delivery_run is not None
    assert trial.delivery_run.state is DeliveryRunState.WAITING_CI
    assert trial.visible_passed is True
    assert trial.hidden_passed is True
    assert trial.error_release is False
    assert "value.txt" in trial.patch
    assert "tests/delivery_visible" not in trial.patch


@pytest.mark.asyncio
async def test_visible_pass_and_hidden_fail_is_an_error_release(
    tmp_path: Path,
) -> None:
    repo, commit = _repo(tmp_path)
    task = load_delivery_task(
        _task(tmp_path, commit, hidden_release_probe=True)
    )

    trial = await run_delivery_trial(
        task,
        repo=repo,
        workdir=tmp_path / "work",
        arm="S0",
        repeat=1,
        phase="pilot",
        protocol_hash="a" * 64,
        model="test-model",
        max_steps=8,
        max_tokens=10_000,
        llm=None,
        implementer_factory=lambda _llm, _command: _Writer(),
        verifier_factory=_no_verifier,
    )

    assert trial.visible_passed is True
    assert trial.hidden_passed is False
    assert trial.error_release is True


@pytest.mark.asyncio
async def test_task_validation_rejects_a_task_that_passes_before_work(
    tmp_path: Path,
) -> None:
    repo, commit = _repo(tmp_path)
    task_dir = _task(tmp_path, commit)
    payload = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    payload["verify_command"] = ["{python}", "-c", "raise SystemExit(0)"]
    payload["hidden_command"] = ["{python}", "-c", "raise SystemExit(0)"]
    (task_dir / "task.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(InvalidDeliveryTask, match="visible fail-first"):
        await validate_delivery_task(
            load_delivery_task(task_dir),
            repo=repo,
            workdir=tmp_path / "validation",
        )


@pytest.mark.asyncio
async def test_source_patch_excludes_protected_test_roots(
    tmp_path: Path,
) -> None:
    repo, commit = _repo(tmp_path)
    task = load_delivery_task(_task(tmp_path, commit))
    workspace = tmp_path / "workspace"
    from bench.harness.workspace import materialize

    empty = tmp_path / "empty.patch"
    empty.write_text("", encoding="utf-8")
    materialize(
        repo=repo,
        base_commit=commit,
        test_patch=empty,
        dest=workspace,
        allow_empty_patch=True,
    )
    (workspace / "value.txt").write_text("2\n", encoding="utf-8")
    visible = workspace / "tests" / "delivery_visible" / "test_value.py"
    visible.parent.mkdir(parents=True)
    visible.write_text("changed\n", encoding="utf-8")

    patch = _source_patch(workspace, task.protected_roots)

    assert "value.txt" in patch
    assert "tests/delivery_visible" not in patch
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run pytest tests/unit/test_delivery_experiment.py -q
```

Expected: FAIL because `bench.delivery.runner` does not exist.

- [ ] **Step 3: Implement the runner and hidden judge**

Create `bench/delivery/runner.py`.

```python
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from app.agents.command_runner import CommandResult, run_command
from apps.code_review_pipeline.change.bundle import build_change_bundle
from apps.code_review_pipeline.change.pipeline import ChangeApproval
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.delivery.adapters.bench_agent import (
    BenchDeliveryImplementer,
    BenchDeliveryVerifier,
)
from apps.code_review_pipeline.delivery.flow import DeliveryFlow
from apps.code_review_pipeline.delivery.models import (
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    FailureCode,
    PushOutcome,
    VerificationEvidence,
)
from apps.code_review_pipeline.delivery.ports import (
    ChangePreparation,
    ImplementerPort,
    PreparationStatus,
    VerifierPort,
)
from bench.delivery.models import (
    SCHEMA_VERSION,
    DeliveryArm,
    DeliveryExperimentRecord,
    DeliveryPhase,
    DeliveryTaskDefinition,
    DeliveryTrial,
)
from bench.delivery.protocol import DeliveryProtocol
from bench.harness.workspace import git, materialize

ImplementerFactory = Callable[[Any, tuple[str, ...]], ImplementerPort]
VerifierFactory = Callable[[Any, tuple[str, ...]], VerifierPort]
_JUDGE_PROTECTED_PATHS = (
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
    "conftest.py",
    "sitecustomize.py",
)


class InvalidDeliveryTask(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryTaskValidation:
    task_id: str
    visible_exit_code: int
    hidden_exit_code: int
    visible_output: str
    hidden_output: str


def _command(argv: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sys.executable if item == "{python}" else item
        for item in argv
    )


async def _run(
    argv: tuple[str, ...],
    *,
    workspace: Path,
) -> CommandResult:
    return await asyncio.to_thread(
        run_command,
        _command(argv),
        cwd=workspace,
    )


def _copy_visible(task: DeliveryTaskDefinition, workspace: Path) -> None:
    for relative in task.visible_tests:
        source = task.task_dir / "visible" / relative
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def _copy_hidden(task: DeliveryTaskDefinition, workspace: Path) -> None:
    for relative in task.hidden_tests:
        source = task.task_dir / "hidden" / relative
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def _commit_visible_fixture(task: DeliveryTaskDefinition, workspace: Path) -> None:
    if task.defect_patch is not None:
        applied = git(
            [
                "apply",
                "--ignore-space-change",
                "--whitespace=nowarn",
                str(task.defect_patch),
            ],
            cwd=workspace,
            check=False,
        )
        if applied.returncode != 0:
            raise RuntimeError(
                f"defect patch failed: {applied.stderr.strip()}"
            )
    _copy_visible(task, workspace)
    git(["add", "-A"], cwd=workspace)
    git(
        ["commit", "-q", "--no-gpg-sign", "-m", "visible fixture"],
        cwd=workspace,
    )


def materialize_delivery_task(
    task: DeliveryTaskDefinition,
    *,
    repo: Path,
    dest: Path,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    empty = dest.parent / f"{dest.name}.empty.patch"
    empty.write_text("", encoding="utf-8")
    materialize(
        repo=repo,
        base_commit=task.base_commit,
        test_patch=empty,
        dest=dest,
        allow_empty_patch=True,
    )
    _commit_visible_fixture(task, dest)
    return dest


async def validate_delivery_task(
    task: DeliveryTaskDefinition,
    *,
    repo: Path,
    workdir: Path,
) -> DeliveryTaskValidation:
    workspace = materialize_delivery_task(
        task,
        repo=repo,
        dest=workdir / task.task_id / "validate",
    )
    visible = await _run(task.verify_command, workspace=workspace)
    if visible.exit_code == 0 or task.fail_first_marker not in visible.output:
        raise InvalidDeliveryTask(
            f"{task.task_id} visible fail-first did not fail with "
            f"{task.fail_first_marker!r}: exit={visible.exit_code}"
        )

    _copy_hidden(task, workspace)
    hidden = await _run(task.hidden_command, workspace=workspace)
    if (
        hidden.exit_code == 0
        or task.hidden_fail_first_marker not in hidden.output
    ):
        raise InvalidDeliveryTask(
            f"{task.task_id} hidden fail-first did not fail with "
            f"{task.hidden_fail_first_marker!r}: exit={hidden.exit_code}"
        )
    return DeliveryTaskValidation(
        task_id=task.task_id,
        visible_exit_code=visible.exit_code,
        hidden_exit_code=hidden.exit_code,
        visible_output=visible.output,
        hidden_output=hidden.output,
    )


class _ExperimentPreparer:
    def __init__(
        self,
        *,
        model: str,
        detail: str,
    ) -> None:
        self._model = model
        self._detail = detail

    async def prepare(
        self,
        *,
        workspace: Path,
        request: DeliveryRequest,
        policy: DeliveryPolicy,
    ) -> ChangePreparation:
        result = await _run(policy.verify_command, workspace=workspace)
        bundle = await asyncio.to_thread(
            build_change_bundle,
            workspace=workspace,
            base_sha=request.base_sha,
            branch=request.branch,
            title=request.title,
            body=request.body,
            verify_command=policy.verify_command,
            verify_exit_code=result.exit_code,
            verify_output=result.output[-4000:],
            model=self._model,
            trace_id=request.trace_id,
        )
        verification = VerificationEvidence(
            command=policy.verify_command,
            exit_code=result.exit_code,
            output_hash=hashlib.sha256(result.output.encode("utf-8")).hexdigest(),
            output_excerpt=result.output[-2000:],
        )
        if result.exit_code != 0:
            status = PreparationStatus.VERIFY_FAILED
        elif not bundle.files:
            status = PreparationStatus.NO_CHANGES
        else:
            status = PreparationStatus.READY
        return ChangePreparation(
            status=status,
            bundle=bundle,
            verification=verification,
            message=result.output[-2000:] or self._detail,
        )


class _AutoApproval:
    async def request(self, *, run_id: str, bundle) -> ChangeApproval:
        return ChangeApproval(
            approver="experiment",
            trace_id=bundle.trace_id,
            bundle_hash=bundle.bundle_hash,
        )


class _DryRunPublisher:
    async def publish(self, *, workspace: Path, bundle, approval) -> PushOutcome:
        return PushOutcome(
            branch=bundle.branch,
            commit_sha="b" * 40,
            pull_request=PullRequestRef(
                number=1,
                url="https://example.invalid/pr/1",
                head_sha="b" * 40,
            ),
        )


class _MemoryRecorder:
    def __init__(self) -> None:
        self.runs: list[DeliveryRun] = []

    async def save(self, run: DeliveryRun) -> None:
        self.runs.append(run)


class _NullAudit:
    async def record(self, **_kwargs) -> None:
        return None


def _source_patch(
    workspace: Path,
    protected_roots: tuple[str, ...],
) -> str:
    git(["add", "-A"], cwd=workspace)
    argv = [
        "diff",
        "--cached",
        "--binary",
        "--no-color",
        "HEAD",
        "--",
        ".",
    ]
    argv.extend(f":(exclude){root}" for root in protected_roots)
    argv.extend(f":(exclude){path}" for path in _JUDGE_PROTECTED_PATHS)
    result = git(argv, cwd=workspace, check=False)
    git(["reset", "-q"], cwd=workspace, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"source patch failed: {result.stderr.strip()}")
    return result.stdout


async def _judge_hidden(
    task: DeliveryTaskDefinition,
    *,
    repo: Path,
    dest: Path,
    source_patch: str,
) -> CommandResult:
    workspace = materialize_delivery_task(task, repo=repo, dest=dest)
    if source_patch.strip():
        patch_path = dest.parent / f"{dest.name}.candidate.patch"
        patch_path.write_text(source_patch, encoding="utf-8")
        applied = git(
            ["apply", "--whitespace=nowarn", str(patch_path)],
            cwd=workspace,
            check=False,
        )
        if applied.returncode != 0:
            return CommandResult(
                exit_code=1,
                output=f"candidate patch failed: {applied.stderr.strip()}",
            )
    _copy_hidden(task, workspace)
    return await _run(task.hidden_command, workspace=workspace)


def _default_implementer(
    llm: Any,
    command: tuple[str, ...],
) -> BenchDeliveryImplementer:
    return BenchDeliveryImplementer(llm=llm, verify_command=command)


def _default_verifier(
    llm: Any,
    command: tuple[str, ...],
) -> BenchDeliveryVerifier:
    return BenchDeliveryVerifier(llm=llm, verify_command=command)


async def run_delivery_trial(
    task: DeliveryTaskDefinition,
    *,
    repo: Path,
    workdir: Path,
    arm: DeliveryArm,
    repeat: int,
    phase: DeliveryPhase,
    protocol_hash: str,
    model: str,
    max_steps: int,
    max_tokens: int,
    llm: Any,
    implementer_factory: ImplementerFactory = _default_implementer,
    verifier_factory: VerifierFactory = _default_verifier,
) -> DeliveryTrial:
    started = time.monotonic()
    trial_id = f"{protocol_hash[:8]}-{phase}-{task.task_id}-{arm}-r{repeat}"
    workspace = workdir / trial_id / "agent"
    recorder = _MemoryRecorder()
    try:
        materialize_delivery_task(task, repo=repo, dest=workspace)
        topology = "single" if arm == "S0" else "verified"
        policy = DeliveryPolicy(
            verify_command=task.verify_command,
            topology=topology,
            max_steps=max_steps,
            max_tokens=max_tokens,
        )
        implementer = implementer_factory(llm, task.verify_command)
        verifier = (
            None
            if arm == "S0"
            else verifier_factory(llm, task.verify_command)
        )
        flow = DeliveryFlow(
            workspace=workspace,
            policy=policy,
            implementer=implementer,
            preparer=_ExperimentPreparer(
                model=model,
                detail=f"{task.task_id}:{arm}:r{repeat}",
            ),
            verifier=verifier,
            approval=_AutoApproval(),
            publisher=_DryRunPublisher(),
            recorder=recorder,
            audit=_NullAudit(),
        )
        request = DeliveryRequest(
            trace_id=trial_id,
            repo="experiment/local",
            base_sha=task.base_commit,
            base_branch="main",
            branch=f"delivery/{trial_id}",
            goal=task.goal,
            title=f"Experiment {task.task_id}",
            body="Synthetic delivery experiment; no remote writes.",
            origin="manual",
        )
        run = await flow.run(request)
        patch = _source_patch(workspace, task.protected_roots)
        hidden = await _judge_hidden(
            task,
            repo=repo,
            dest=workdir / trial_id / "judge",
            source_patch=patch,
        )
        visible_passed = run.state is DeliveryRunState.WAITING_CI
        hidden_passed = hidden.exit_code == 0
        revise_attempted = any(
            verdict.decision == "revise"
            for verdict in run.verifier_verdicts
        )
        revise_recovered = (
            revise_attempted
            and visible_passed
            and bool(run.verifier_verdicts)
            and run.verifier_verdicts[-1].decision == "approved"
        )
        verifier_invalid = (
            run.failure is not None
            and run.failure.code is FailureCode.VERIFIER_INVALID
        )
        return DeliveryTrial(
            schema_version=SCHEMA_VERSION,
            trial_id=trial_id,
            protocol_hash=protocol_hash,
            phase=phase,
            task_id=task.task_id,
            arm=arm,
            repeat=repeat,
            model=model,
            status="valid",
            delivery_run=run,
            usage=run.usage,
            visible_passed=visible_passed,
            hidden_passed=hidden_passed,
            error_release=visible_passed and not hidden_passed,
            revise_attempted=revise_attempted,
            revise_recovered=revise_recovered,
            verifier_invalid=verifier_invalid,
            patch=patch,
            visible_output=(
                run.bundle.verify_output
                if run.bundle is not None
                else ""
            ),
            hidden_output=hidden.output,
            error="",
            wall_clock_s=time.monotonic() - started,
        )
    except Exception as exc:
        return DeliveryTrial(
            schema_version=SCHEMA_VERSION,
            trial_id=trial_id,
            protocol_hash=protocol_hash,
            phase=phase,
            task_id=task.task_id,
            arm=arm,
            repeat=repeat,
            model=model,
            status="error",
            delivery_run=None,
            usage=DeliveryUsage(),
            visible_passed=False,
            hidden_passed=False,
            error_release=False,
            revise_attempted=False,
            revise_recovered=False,
            verifier_invalid=False,
            patch="",
            visible_output="",
            hidden_output="",
            error=f"{type(exc).__name__}: {exc}",
            wall_clock_s=time.monotonic() - started,
        )


def _write_record(path: Path, record: DeliveryExperimentRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def run_delivery_experiment(
    protocol: DeliveryProtocol,
    *,
    repo: Path,
    workdir: Path,
    out: Path,
    phase: DeliveryPhase,
    llm: Any,
    implementer_factory: ImplementerFactory = _default_implementer,
    verifier_factory: VerifierFactory = _default_verifier,
) -> DeliveryExperimentRecord:
    record = DeliveryExperimentRecord(
        schema_version=SCHEMA_VERSION,
        protocol_id=protocol.protocol_id,
        protocol_hash=protocol.config_hash,
        phase=phase,
        started_at=datetime.now(UTC).isoformat(),
        trials=(),
    )
    trials: list[DeliveryTrial] = []
    _write_record(out, record)
    for task in protocol.tasks:
        for arm in ("S0", "S1"):
            for repeat in range(1, protocol.repeats.for_phase(phase) + 1):
                trial = await run_delivery_trial(
                    task,
                    repo=repo,
                    workdir=workdir,
                    arm=arm,
                    repeat=repeat,
                    phase=phase,
                    protocol_hash=protocol.config_hash,
                    model=protocol.model.model,
                    max_steps=protocol.budget.max_steps,
                    max_tokens=protocol.budget.max_tokens,
                    llm=llm,
                    implementer_factory=implementer_factory,
                    verifier_factory=verifier_factory,
                )
                trials.append(trial)
                record = DeliveryExperimentRecord(
                    schema_version=SCHEMA_VERSION,
                    protocol_id=protocol.protocol_id,
                    protocol_hash=protocol.config_hash,
                    phase=phase,
                    started_at=record.started_at,
                    trials=tuple(trials),
                )
                _write_record(out, record)
    return record


__all__ = [
    "DeliveryTaskValidation",
    "InvalidDeliveryTask",
    "materialize_delivery_task",
    "run_delivery_experiment",
    "run_delivery_trial",
    "validate_delivery_task",
]
```

- [ ] **Step 4: Run the runner tests**

Run:

```powershell
uv run pytest tests/unit/test_delivery_experiment.py tests/unit/test_delivery_protocol.py -q
uv run ruff check bench/delivery/runner.py tests/unit/test_delivery_experiment.py
```

Expected: all tests pass. The hidden judge never sees hidden files during the
Implementer or Verifier run, and the source patch excludes `tests/`.

- [ ] **Step 5: Commit**

```powershell
git add bench/delivery/runner.py tests/unit/test_delivery_experiment.py
git commit -m "feat: run and judge governed delivery trials"
```

---

### Task 7: Add the Paired Report and Offline-First Command Entry

**Files:**
- Create: `bench/delivery/report.py`
- Create: `bench/run_delivery_experiment.py`
- Modify: `tests/unit/test_delivery_protocol.py`

**Interfaces:**
- Consumes: `DeliveryExperimentRecord`, `DeliveryProtocol`, `run_delivery_experiment`, `validate_delivery_task`.
- Produces: `ArmMetrics`, `DeliveryReport`, `build_delivery_report(record, protocol)`, CLI commands `validate` and `run`.

- [ ] **Step 1: Write the failing report and CLI tests**

Append this code to `tests/unit/test_delivery_protocol.py`.

```python
import bench.run_delivery_experiment as delivery_cli
from apps.code_review_pipeline.delivery.models import DeliveryUsage
from bench.delivery.models import (
    DeliveryExperimentRecord,
    DeliveryTrial,
)
from bench.delivery.report import build_delivery_report


def _trial(
    protocol,
    task_id: str,
    arm: str,
    repeat: int,
    *,
    hidden_passed: bool,
    tokens: int,
) -> DeliveryTrial:
    return DeliveryTrial(
        schema_version=1,
        trial_id=f"{task_id}-{arm}-{repeat}",
        protocol_hash=protocol.config_hash,
        phase="pilot",
        task_id=task_id,
        arm=arm,
        repeat=repeat,
        model=protocol.model.model,
        status="valid",
        delivery_run=None,
        usage=DeliveryUsage(
            llm_calls=1,
            prompt_tokens=tokens,
            completion_tokens=0,
        ),
        visible_passed=True,
        hidden_passed=hidden_passed,
        error_release=not hidden_passed,
        revise_attempted=False,
        revise_recovered=False,
        verifier_invalid=False,
        patch="diff --git a/x b/x\n",
        visible_output="visible ok",
        hidden_output="hidden ok" if hidden_passed else "hidden failed",
        error="",
        wall_clock_s=1.0,
    )


def _record(protocol, *, s1_hidden: bool = True):
    trials: list[DeliveryTrial] = []
    for task in protocol.tasks:
        for repeat in (1, 2):
            trials.append(
                _trial(
                    protocol,
                    task.task_id,
                    "S0",
                    repeat,
                    hidden_passed=repeat == 2,
                    tokens=100,
                )
            )
            trials.append(
                _trial(
                    protocol,
                    task.task_id,
                    "S1",
                    repeat,
                    hidden_passed=s1_hidden,
                    tokens=150,
                )
            )
    return DeliveryExperimentRecord(
        schema_version=1,
        protocol_id=protocol.protocol_id,
        protocol_hash=protocol.config_hash,
        phase="pilot",
        started_at="2026-10-27T00:00:00+00:00",
        trials=tuple(trials),
    )


def test_report_calculates_paired_metrics_and_default_gate() -> None:
    protocol = load_delivery_protocol(PROTOCOL_PATH, tasks_root=TASKS_ROOT)
    report = build_delivery_report(_record(protocol), protocol)

    assert report.s0.hidden_pass_rate == 0.5
    assert report.s1.hidden_pass_rate == 1.0
    assert report.s0.error_release_rate == 0.5
    assert report.s1.error_release_rate == 0.0
    assert report.paired_token_delta_s1_minus_s0 == 50.0
    assert report.s1.tokens_per_pass == 150.0
    assert report.gate_failures == ()
    assert report.s1_default_eligible is True


def test_report_refuses_s1_when_hidden_pass_rate_drops() -> None:
    protocol = load_delivery_protocol(PROTOCOL_PATH, tasks_root=TASKS_ROOT)
    report = build_delivery_report(
        _record(protocol, s1_hidden=False),
        protocol,
    )

    assert any("hidden pass" in item for item in report.gate_failures)
    assert any("error release" in item for item in report.gate_failures)
    assert report.s1_default_eligible is False


@pytest.mark.asyncio
async def test_cli_validate_is_offline_and_injectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_validate(*_args, **_kwargs):
        return ()

    monkeypatch.setattr(delivery_cli, "validate_all_tasks", fake_validate)

    code = await delivery_cli.main(
        [
            "validate",
            "--protocol",
            str(PROTOCOL_PATH),
            "--tasks-root",
            str(TASKS_ROOT),
        ]
    )

    assert code == 0


def test_cli_defaults_to_offline_mode() -> None:
    args = delivery_cli.parse_args(
        ["run", "--phase", "pilot", "--protocol", str(PROTOCOL_PATH)]
    )

    assert args.mode == "offline"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run pytest tests/unit/test_delivery_protocol.py -q
```

Expected: FAIL because `bench.delivery.report` and
`bench.run_delivery_experiment` do not exist.

- [ ] **Step 3: Implement the paired report**

Create `bench/delivery/report.py`.

```python
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from bench.delivery.models import DeliveryExperimentRecord, DeliveryTrial
from bench.delivery.protocol import DeliveryProtocol


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


@dataclass(frozen=True)
class ArmMetrics:
    arm: str
    trials: int
    valid_trials: int
    hidden_passes: int
    visible_passes: int
    error_releases: int
    total_tokens: int
    revise_attempts: int
    revise_recoveries: int
    verifier_invalid: int
    run_states: tuple[tuple[str, int], ...]
    failure_codes: tuple[tuple[str, int], ...]

    @property
    def hidden_pass_rate(self) -> float | None:
        return _rate(self.hidden_passes, self.valid_trials)

    @property
    def error_release_rate(self) -> float | None:
        return _rate(self.error_releases, self.visible_passes)

    @property
    def tokens_per_pass(self) -> float | None:
        return (
            self.total_tokens / self.hidden_passes
            if self.hidden_passes
            else None
        )

    @property
    def revision_recovery_rate(self) -> float | None:
        return _rate(self.revise_recoveries, self.revise_attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "trials": self.trials,
            "valid_trials": self.valid_trials,
            "hidden_passes": self.hidden_passes,
            "hidden_pass_rate": self.hidden_pass_rate,
            "visible_passes": self.visible_passes,
            "error_releases": self.error_releases,
            "error_release_rate": self.error_release_rate,
            "total_tokens": self.total_tokens,
            "tokens_per_pass": self.tokens_per_pass,
            "revise_attempts": self.revise_attempts,
            "revise_recoveries": self.revise_recoveries,
            "revision_recovery_rate": self.revision_recovery_rate,
            "verifier_invalid": self.verifier_invalid,
            "run_states": dict(self.run_states),
            "failure_codes": dict(self.failure_codes),
        }


def _arm_metrics(arm: str, trials: tuple[DeliveryTrial, ...]) -> ArmMetrics:
    selected = tuple(trial for trial in trials if trial.arm == arm)
    valid = tuple(trial for trial in selected if trial.status == "valid")
    states: dict[str, int] = {}
    failures: dict[str, int] = {}
    for trial in selected:
        key = trial.run_state or trial.status
        states[key] = states.get(key, 0) + 1
        if trial.failure_code:
            failures[trial.failure_code] = failures.get(trial.failure_code, 0) + 1
    return ArmMetrics(
        arm=arm,
        trials=len(selected),
        valid_trials=len(valid),
        hidden_passes=sum(trial.hidden_passed for trial in valid),
        visible_passes=sum(trial.visible_passed for trial in valid),
        error_releases=sum(trial.error_release for trial in valid),
        total_tokens=sum(trial.usage.total_tokens for trial in valid),
        revise_attempts=sum(trial.revise_attempted for trial in valid),
        revise_recoveries=sum(trial.revise_recovered for trial in valid),
        verifier_invalid=sum(trial.verifier_invalid for trial in valid),
        run_states=tuple(sorted(states.items())),
        failure_codes=tuple(sorted(failures.items())),
    )


def _paired_token_delta(
    trials: tuple[DeliveryTrial, ...],
) -> float | None:
    by_cell = {
        (trial.task_id, trial.arm, trial.repeat): trial
        for trial in trials
        if trial.status == "valid"
    }
    deltas: list[int] = []
    for (task_id, arm, repeat), trial in by_cell.items():
        if arm != "S1":
            continue
        baseline = by_cell.get((task_id, "S0", repeat))
        if baseline is None:
            continue
        deltas.append(
            trial.usage.total_tokens - baseline.usage.total_tokens
        )
    return sum(deltas) / len(deltas) if deltas else None


@dataclass(frozen=True)
class DeliveryReport:
    protocol_hash: str
    phase: str
    s0: ArmMetrics
    s1: ArmMetrics
    paired_token_delta_s1_minus_s0: float | None
    gate_failures: tuple[str, ...]
    s1_default_eligible: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_hash": self.protocol_hash,
            "phase": self.phase,
            "s0": self.s0.to_dict(),
            "s1": self.s1.to_dict(),
            "paired_token_delta_s1_minus_s0": (
                self.paired_token_delta_s1_minus_s0
            ),
            "gate_failures": list(self.gate_failures),
            "s1_default_eligible": self.s1_default_eligible,
        }

    def to_markdown(self) -> str:
        def pct(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.1%}"

        def number(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.1f}"

        lines = [
            f"# Delivery experiment report ({self.phase})",
            "",
            f"- protocol_hash: `{self.protocol_hash}`",
            f"- hidden pass: S0 {pct(self.s0.hidden_pass_rate)}, "
            f"S1 {pct(self.s1.hidden_pass_rate)}",
            f"- error release: S0 {pct(self.s0.error_release_rate)}, "
            f"S1 {pct(self.s1.error_release_rate)}",
            f"- tokens/pass: S0 {number(self.s0.tokens_per_pass)}, "
            f"S1 {number(self.s1.tokens_per_pass)}",
            f"- revision recovery: S1 "
            f"{pct(self.s1.revision_recovery_rate)}",
            f"- S1 incremental tokens: "
            f"{number(self.paired_token_delta_s1_minus_s0)}",
            f"- verifier invalid: {self.s1.verifier_invalid}",
            f"- default eligible: {str(self.s1_default_eligible).lower()}",
            "",
            "## Gates",
            "",
        ]
        if self.gate_failures:
            lines.extend(f"- FAIL: {item}" for item in self.gate_failures)
        else:
            lines.append("- PASS: all pre-registered gates")
        return "\n".join(lines) + "\n"


def build_delivery_report(
    record: DeliveryExperimentRecord,
    protocol: DeliveryProtocol,
) -> DeliveryReport:
    s0 = _arm_metrics("S0", record.trials)
    s1 = _arm_metrics("S1", record.trials)
    failures: list[str] = []

    if record.protocol_hash != protocol.config_hash:
        failures.append("record protocol hash differs from the loaded protocol")
    if any(
        trial.protocol_hash != protocol.config_hash
        for trial in record.trials
    ):
        failures.append("trial protocol hash mismatch")

    errors = sum(trial.status == "error" for trial in record.trials)
    if errors > protocol.gates.max_unattributed_errors:
        failures.append(f"unattributed errors: {errors}")

    if protocol.gates.require_all_cells:
        expected = {
            (task.task_id, arm.name, repeat)
            for task in protocol.tasks
            for arm in protocol.arms
            for repeat in range(1, protocol.repeats.for_phase(record.phase) + 1)
        }
        actual = {
            (trial.task_id, trial.arm, trial.repeat)
            for trial in record.trials
            if trial.status == "valid"
        }
        if actual != expected:
            failures.append(
                f"required cell coverage mismatch: "
                f"missing={len(expected - actual)}, extra={len(actual - expected)}"
            )
        repeat_counts = Counter(
            (trial.task_id, trial.arm, trial.repeat)
            for trial in record.trials
            if trial.status == "valid"
        )
        duplicated = [
            cell for cell in expected if repeat_counts[cell] > 1
        ]
        if duplicated:
            failures.append(
                f"duplicate required cells: {len(duplicated)}"
            )
        per_arm = Counter(
            (trial.task_id, trial.arm)
            for trial in record.trials
            if trial.status == "valid"
        )
        for task in protocol.tasks:
            for arm in protocol.arms:
                count = per_arm[(task.task_id, arm.name)]
                if count < protocol.min_valid_repeats_per_cell:
                    failures.append(
                        f"valid repeats below minimum: "
                        f"{task.task_id}/{arm.name}={count}"
                    )

    if record.phase == "pilot":
        rate = s0.hidden_pass_rate
        if (
            rate is None
            or rate <= protocol.gates.pilot_baseline_min_exclusive
            or rate >= protocol.gates.pilot_baseline_max_exclusive
        ):
            failures.append(
                f"pilot S0 baseline is not discriminative: {rate!r}"
            )

    if (
        s0.hidden_pass_rate is None
        or s1.hidden_pass_rate is None
        or s1.hidden_pass_rate
        < s0.hidden_pass_rate - protocol.gates.max_hidden_pass_drop
    ):
        failures.append("S1 hidden pass rate dropped beyond the allowed margin")

    if protocol.gates.require_error_release_improvement:
        if (
            s0.error_release_rate is None
            or s1.error_release_rate is None
            or s1.error_release_rate >= s0.error_release_rate
        ):
            failures.append(
                "S1 did not improve the visible-pass/hidden-fail error release rate"
            )

    return DeliveryReport(
        protocol_hash=protocol.config_hash,
        phase=record.phase,
        s0=s0,
        s1=s1,
        paired_token_delta_s1_minus_s0=_paired_token_delta(record.trials),
        gate_failures=tuple(failures),
        s1_default_eligible=not failures,
    )


__all__ = [
    "ArmMetrics",
    "DeliveryReport",
    "build_delivery_report",
]
```

- [ ] **Step 4: Implement the offline-first CLI**

Create `bench/run_delivery_experiment.py`.

```python
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.config  # noqa: F401  (side effect: load_dotenv())

from app.agents.provider import LLMClient, LLMConfig  # noqa: E402
from bench.delivery.models import DeliveryTaskDefinition  # noqa: E402
from bench.delivery.protocol import (  # noqa: E402
    DeliveryProtocol,
    load_delivery_protocol,
)
from bench.delivery.report import (  # noqa: E402
    DeliveryReport,
    build_delivery_report,
)
from bench.delivery.runner import (  # noqa: E402
    DeliveryTaskValidation,
    run_delivery_experiment,
    validate_delivery_task,
)

DEFAULT_PROTOCOL = (
    ROOT
    / "bench"
    / "delivery"
    / "protocols"
    / "repository-delivery-s0-s1-v1.json"
)
logger = logging.getLogger("bench.delivery.cli")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pre-registered S0/S1 repository-delivery experiment"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="offline fail-first validation")
    validate.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    validate.add_argument("--tasks-root", type=Path, default=None)
    validate.add_argument(
        "--workdir",
        type=Path,
        default=ROOT / ".bench-delivery" / "validation",
    )

    run = sub.add_parser("run", help="validate, then run pilot/formal trials")
    run.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    run.add_argument("--tasks-root", type=Path, default=None)
    run.add_argument("--phase", choices=["pilot", "formal"], required=True)
    run.add_argument(
        "--mode",
        choices=["offline", "live"],
        default="offline",
        help="offline is the default; live spends real model tokens",
    )
    run.add_argument(
        "--out",
        type=Path,
        default=ROOT / ".bench-delivery" / "latest",
    )
    run.add_argument(
        "--workdir",
        type=Path,
        default=ROOT / ".bench-delivery" / "work",
    )
    return parser.parse_args(argv)


async def validate_all_tasks(
    protocol: DeliveryProtocol,
    *,
    repo: Path,
    workdir: Path,
) -> tuple[DeliveryTaskValidation, ...]:
    return tuple(
        [
            await validate_delivery_task(task, repo=repo, workdir=workdir)
            for task in protocol.tasks
        ]
    )


def _load(args: argparse.Namespace) -> DeliveryProtocol:
    return load_delivery_protocol(
        args.protocol,
        tasks_root=args.tasks_root,
    )


def build_live_llm(protocol: DeliveryProtocol) -> LLMClient:
    config = LLMConfig.from_env("LLM")
    if config.provider.lower() != protocol.model.provider.lower():
        raise ValueError(
            "LLM_PROVIDER does not match the frozen protocol: "
            f"{config.provider!r} != {protocol.model.provider!r}"
        )
    config = dataclasses.replace(
        config,
        model=protocol.model.model,
        temperature=protocol.model.temperature,
        max_tokens=protocol.model.max_output_tokens,
    )
    return LLMClient(config)


def _write_outputs(
    *,
    record,
    report: DeliveryReport,
    out: Path,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "record.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "report.md").write_text(report.to_markdown(), encoding="utf-8")


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args(argv)
    try:
        protocol = _load(args)
        validations = await validate_all_tasks(
            protocol,
            repo=ROOT,
            workdir=args.workdir,
        )
        for item in validations:
            print(f"{item.task_id}: visible={item.visible_exit_code} hidden={item.hidden_exit_code}")
        if args.command == "validate":
            print(f"protocol_hash={protocol.config_hash}")
            return 0
        if args.mode == "offline":
            print("offline validation passed; no model calls or trials were run")
            return 0

        llm = build_live_llm(protocol)
        record = await run_delivery_experiment(
            protocol,
            repo=ROOT,
            workdir=args.workdir,
            out=args.out / "record.json",
            phase=args.phase,
            llm=llm,
        )
        report = build_delivery_report(record, protocol)
        _write_outputs(record=record, report=report, out=args.out)
        print(report.to_markdown())
        return 0
    except Exception as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 5: Run report, CLI, and protocol tests**

Run:

```powershell
uv run pytest tests/unit/test_delivery_protocol.py tests/unit/test_delivery_experiment.py -q
uv run ruff check bench/delivery bench/run_delivery_experiment.py tests/unit/test_delivery_protocol.py
```

Expected: all pass. `run` without `--mode live` only validates tasks and never
constructs an LLM client.

- [ ] **Step 6: Commit**

```powershell
git add bench/delivery/report.py bench/run_delivery_experiment.py tests/unit/test_delivery_protocol.py
git commit -m "feat: report governed delivery experiment gates"
```

---

### Task 8: Lock the Public Claim Boundary and Run the Full Gate

**Files:**
- Modify: `README.md`
- Modify: `CONTEXT.md`
- Modify: `delivery/定位与减法清单.md`
- Modify: `docs/adr/adr-031-governed-repository-delivery-agent.md`
- Modify: `docs/superpowers/specs/2026-10-27-governed-repo-delivery-agent-design.md`

**Interfaces:**
- Consumes: implemented S0/S1 code and the pre-registered protocol.
- Produces: public documentation that distinguishes implemented capability,
  experimental topology, and unrun/unproven outcomes.

- [ ] **Step 1: Add the exact S1 capability boundary to ADR-031**

Replace the ADR status line with:

```markdown
状态：已接受；S0 默认路径已实现，S1 为默认关闭的预注册实验
```

Append these decisions and consequences to the ADR:

```markdown
## S1 复核拓扑

9. `topology="verified"` 复用 S0 的 Implementer、bundle、审批、推送和合并路径。
10. Verifier 是独立只读会话，只能 `read_file` / `search` / `run_tests`。
11. Verifier 的 `approved` 只把 run 推进到人工推送审批，不产生远端写。
12. Verifier 无效、缺证据或引用旧 bundle 时 fail-closed 为 `VERIFIER_INVALID`。
13. 测试失败与 Verifier 打回共享 `max_revisions=1`；不存在第三次修改。
14. S1 在预注册 pilot/formal 报告满足门禁前不得成为默认拓扑。

## S1 的实验边界

- `bench/delivery/` 只比较同一模型、同一任务集、同一预算、同一 hidden judge
  下的 S0 与 S1。
- S1 的价值指标是降低“visible 通过、hidden 失败”的错误放行率，不是 agent
  自我报告的完成率。
- v5/v8 多智能体结果不能进入本实验报告。
- CI 排障仍不在本 ADR 的实现范围内；PR 创建后的 CI 失败继续转人工。
```

This wording is deliberately narrower than “多智能体协作”: S1 is one
Implementer plus one read-only Verifier, not an open-ended handoff graph.

- [ ] **Step 2: Update README with implemented versus experimental status**

In the governed repository delivery subsection created by S0, add:

```markdown
**当前状态（2026-10-27）**：S0 单 Implementer 是默认路径；S1 只读 Verifier
已经实现为显式 `topology="verified"` 的实验拓扑，默认关闭。S1 是否值得默认开启，
由 `bench/delivery/protocols/repository-delivery-s0-s1-v1.json` 冻结的
pilot → formal 门禁决定；在报告过门禁前，不宣称 Verifier 有增益。

S1 不是无边界多智能体协作：它只有一个可写 Implementer、一个独立只读 Verifier，
以及最多一次修复回边。Verifier 不能写文件、审批、推送或合并。
```

Immediately after the experiment command block, add:

````markdown
```powershell
uv run python bench/run_delivery_experiment.py validate --protocol bench/delivery/protocols/repository-delivery-s0-s1-v1.json
uv run python bench/run_delivery_experiment.py run --protocol bench/delivery/protocols/repository-delivery-s0-s1-v1.json --phase pilot --mode live --out .bench-delivery/pilot
```

`run` 默认 `--mode offline`，只做 fail-first 校验；只有显式 `--mode live`
才会调用真实模型。CI 不跑 live。
````

Do not add a claim that S1 improves pass rate, error-release rate, or cost until
a formal report with the same `protocol_hash` exists.

- [ ] **Step 3: Extend the glossary and subtraction ledger**

Append to the `## 仓库交付` section in `CONTEXT.md`:

```markdown
**S0 单实现体 (single topology)**:
默认交付拓扑：一个可写 Implementer 加确定性验证、人工审批、推送与合并门禁。
_Avoid_: 多智能体、自动交付

**S1 只读复核 (verified topology)**:
预注册实验拓扑：S0 加一个独立只读 Verifier，允许多至一次修复回边。
它默认关闭，`approved` 也只进入人工推送审批。
_Avoid_: 双 Agent 自动合并、Verifier 判决者

**错误放行率 (error-release rate)**:
visible 验证通过并进入推送门、但 hidden judge 失败的 trial 比例。它是 S1 的
主要风险指标，不是整体通过率。
_Avoid_: 失败率、幻觉率
```

Append this row to the decision table in `delivery/定位与减法清单.md`:

```markdown
| 13 | **S1 只读 Verifier 实验**（S0 + 一次有界复核） | 用 hidden judge 检验 Verifier 是否降低“visible 绿、真实未达成”的错误放行；默认拓扑不变 | 规格已冻结，S0/S1 共用同一交付与预算契约；只有同 `protocol_hash` 的 pilot/formal 报告过门禁后才讨论默认开启 |
```

Also append this to the “被问多智能体到底强不强” guidance:

```markdown
- 若被问 S1：先给 hidden pass rate、error-release rate、tokens/pass 与 protocol_hash。
  S1 是按职责/权限拆分的单次只读复核实验，不是无边界多 Agent 协作；没有过门禁的
  报告时，答案只能是“已实现实验拓扑，尚未证明增益”。
```

- [ ] **Step 4: Mark the design as confirmed**

Set the spec header to the only allowed final state:

```markdown
- 状态：已确认
```

- [ ] **Step 5: Run the claim-boundary scans**

Run:

```powershell
rg -n "S1.*默认开启|Verifier.*自动合并|多智能体协作.*S1|已证明.*Verifier" README.md CONTEXT.md delivery docs/adr/adr-031-governed-repository-delivery-agent.md
rg -n "^- 状态：" docs/superpowers/specs/2026-10-27-governed-repo-delivery-agent-design.md
```

Expected: the first command prints no matches; the second prints only
`- 状态：已确认`.

- [ ] **Step 6: Run the full implementation gate**

Run:

```powershell
uv run pytest tests/unit -q
uv run ruff check bench apps/code_review_pipeline/delivery tests/unit
git diff --check
```

Expected: all unit tests pass, Ruff reports no errors, and `git diff --check`
reports no whitespace errors.

- [ ] **Step 7: Commit**

```powershell
git add README.md CONTEXT.md delivery/定位与减法清单.md docs/adr/adr-031-governed-repository-delivery-agent.md docs/superpowers/specs/2026-10-27-governed-repo-delivery-agent-design.md
git commit -m "docs: bound governed delivery S1 claims"
```

---

## Final Self-Review

Before handing the plan to an executor, run these three checks against both the
spec and this plan:

1. **Spec coverage:** every S1 requirement maps to Task 1-4; task-set/protocol
   freezing maps to Task 5; hidden judging and run records map to Task 6; report
   gates and CLI map to Task 7; claim boundaries map to Task 8.
2. **Placeholder scan:** no deferred-detail marker, angle-bracket configuration
   placeholder, hand-waving validation instruction, or cross-reference that
   replaces required content remains.
3. **Type consistency:** `VerifierPort.review`, `VerifierVerdict`,
   `DeliveryTrial.usage`, `DeliveryProtocol.config_hash`,
   `run_delivery_trial`, `validate_delivery_task`, and `build_delivery_report`
   use the same names and signatures in every task that references them.
