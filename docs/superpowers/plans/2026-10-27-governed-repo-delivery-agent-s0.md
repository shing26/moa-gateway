# Governed Repository Delivery Agent S0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the default S0 delivery loop: one bounded Implementer changes an isolated workspace, deterministic verification produces one `ChangeBundle`, a human approves the exact bundle, GitHub receives one branch/commit/PR, and the PR is registered in the existing merge channel.

**Architecture:** `DeliveryFlow.run(DeliveryRequest)` orchestrates immutable dataclasses and narrow protocols. Existing `ChangePipeline`, the production tool loop, `GitHubClient`, `MergeStore`, and the merge worker remain the only side-effect and state-machine implementations. S0 supports `topology="single"` only; the complete state enum is installed now so the separately planned S1 Verifier can use the same contract without redesigning S0.

**Tech Stack:** Python 3.12, frozen dataclasses, `typing.Protocol`, asyncio, pytest, Ruff, existing LiteLLM/tool loop, GitHub REST client, in-memory/Postgres `MergeStore`, hash-chained audit WAL.

**Spec:** `docs/superpowers/specs/2026-10-27-governed-repo-delivery-agent-design.md`

## Global Constraints

- Default topology is `single`; S0 must reject `verified` rather than silently downgrade.
- `max_revisions` is exactly `1`; an invalid policy is a configuration error.
- Implementer tools are exactly `read_file`, `write_file`, `search`, and `run_tests` when a verify command exists.
- Remote writes occur only after deterministic verification and a matching human `ChangeApproval`.
- The only remote writes are `create_ref`, `create_commit`, and `create_pull_request`; never force push, delete a branch, or write the base branch.
- Approval is bound to `trace_id`, canonical `bundle_hash`, all file hashes, the full untruncated `patch_hash`, title, body, model, and verify command.
- CI failure is terminal for this product flow except that the existing merge poller may observe a later green rerun; S0 never edits code in response to CI failure.
- No new database table, queue, dashboard, channel, RAG path, MCP endpoint, Redis/Postgres checkpointer, PR/Issue/Fork score, Planner, concurrent worker, or generic Supervisor.
- Product code under `apps/code_review_pipeline/delivery/` and `app/agents/` must not import `bench/`.
- `DeliveryRun` is immutable and uses `to_dict()` as its only serialization exit.
- A new execution gets a new `run_id`; records are never overwritten.
- Use `uv run pytest ...` and `uv run ruff check ...` for every verification step.
- Do not include `.scratch/*` in any commit.
- Treat the current uncommitted `apps/code_review_pipeline/change/*` and `tests/unit/test_change_pipeline.py` hardening as a prerequisite commit owned by the user; do not mix it with this plan's commits.

## File Structure

**Create**

- `docs/adr/adr-031-governed-repository-delivery-agent.md`
- `apps/code_review_pipeline/delivery/__init__.py`
- `apps/code_review_pipeline/delivery/models.py`
- `apps/code_review_pipeline/delivery/state.py`
- `apps/code_review_pipeline/delivery/ports.py`
- `apps/code_review_pipeline/delivery/flow.py`
- `apps/code_review_pipeline/delivery/run_store.py`
- `apps/code_review_pipeline/delivery/audit.py`
- `apps/code_review_pipeline/delivery/workspace.py`
- `apps/code_review_pipeline/delivery/adapters/__init__.py`
- `apps/code_review_pipeline/delivery/adapters/production_agent.py`
- `apps/code_review_pipeline/delivery/adapters/github.py`
- `apps/code_review_pipeline/change/refs.py`
- `tests/unit/test_delivery_models.py`
- `tests/unit/test_delivery_state.py`
- `tests/unit/test_delivery_flow.py`
- `tests/unit/test_delivery_store.py`
- `tests/unit/test_structured_tool_loop.py`
- `tests/unit/test_delivery_production_agent.py`
- `tests/unit/test_delivery_github_adapter.py`
- `tests/unit/test_delivery_workspace.py`
- `tests/unit/test_delivery_cli.py`
- `tests/unit/test_delivery_import_boundaries.py`

**Modify**

- `README.md`
- `CONTEXT.md`
- `delivery/定位与减法清单.md`
- `.env.template`
- `app/config.py`
- `app/agents/stubs.py`
- `apps/code_review_pipeline/change/__init__.py`
- `apps/code_review_pipeline/change/pipeline.py`
- `apps/code_review_pipeline/routing/github_client.py`
- `app/cli.py`
- `tests/unit/test_change_pipeline.py`
- `tests/unit/test_config.py`
- `tests/unit/test_github_client_trust_env.py`

**Responsibility map**

- `models.py`: immutable data contracts and serialization only.
- `change/refs.py`: neutral GitHub identity shared by the client, bundle layer, and delivery contracts.
- `state.py`: outer delivery state transition table only.
- `ports.py`: protocol seams and port-only result types.
- `flow.py`: deterministic orchestration; no concrete GitHub, LLM, file, or WAL access.
- `run_store.py`: JSON Run Record persistence and current merge view.
- `audit.py`: translation from delivery transitions to the existing audit WAL.
- `workspace.py`: GitHub archive materialization into a local Git baseline.
- `adapters/production_agent.py`: prompt construction and mapping from the existing tool loop to `AgentAttempt`.
- `adapters/github.py`: mapping `DeliveryFlow` ports to `ChangePipeline`, `GitHubClient`, and `MergeStore`.
- `app/agents/stubs.py`: reusable structured tool-loop primitive; existing agent behavior stays unchanged.

---

### Task 1: Lock the Product Boundary and Delivery Configuration

**Files:**
- Create: `docs/adr/adr-031-governed-repository-delivery-agent.md`
- Modify: `README.md`
- Modify: `CONTEXT.md`
- Modify: `delivery/定位与减法清单.md`
- Modify: `.env.template:258-272`
- Modify: `app/config.py:258-289`
- Modify: `app/config.py:382-416`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: existing `Settings` construction and `_parse_int`.
- Produces: `settings.delivery_topology: str`, `settings.delivery_max_steps: int`, `settings.delivery_max_tokens: int`, `settings.delivery_run_record_dir: str`.

- [ ] **Step 1: Write the failing configuration tests**

Add these keys to the `_clean_env` fixture in `tests/unit/test_config.py`.

```python
        "DELIVERY_TOPOLOGY",
        "DELIVERY_MAX_STEPS",
        "DELIVERY_MAX_TOKENS",
        "DELIVERY_RUN_RECORD_DIR",
```

Append these tests.

```python
def test_delivery_defaults_are_single_and_bounded() -> None:
    s = Settings()
    assert s.delivery_topology == "single"
    assert s.delivery_max_steps == 40
    assert s.delivery_max_tokens == 400_000
    assert s.delivery_run_record_dir == "data/delivery-runs"


def test_delivery_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_TOPOLOGY", "verified")
    monkeypatch.setenv("DELIVERY_MAX_STEPS", "12")
    monkeypatch.setenv("DELIVERY_MAX_TOKENS", "9000")
    monkeypatch.setenv("DELIVERY_RUN_RECORD_DIR", "tmp/runs")
    s = Settings()
    assert s.delivery_topology == "verified"
    assert s.delivery_max_steps == 12
    assert s.delivery_max_tokens == 9000
    assert s.delivery_run_record_dir == "tmp/runs"


def test_delivery_rejects_unknown_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_TOPOLOGY", "swarm")
    with pytest.raises(ValueError, match="DELIVERY_TOPOLOGY"):
        Settings()


def test_delivery_rejects_nonpositive_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_MAX_STEPS", "0")
    with pytest.raises(ValueError, match="DELIVERY_MAX_STEPS"):
        Settings()
    monkeypatch.setenv("DELIVERY_MAX_STEPS", "40")
    monkeypatch.setenv("DELIVERY_MAX_TOKENS", "0")
    with pytest.raises(ValueError, match="DELIVERY_MAX_TOKENS"):
        Settings()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -q`

Expected: FAIL because `Settings` has no `delivery_topology` attribute and does not validate the new names.

- [ ] **Step 3: Implement the configuration fields and validation**

Add the fields beside the existing change-agent settings.

```python
        # ── 受治理仓库交付（ADR-031）────────────────────────────────────
        self.delivery_topology: str = (
            os.getenv("DELIVERY_TOPOLOGY", "single") or "single"
        ).strip().lower()
        self.delivery_max_steps: int = _parse_int(
            os.getenv("DELIVERY_MAX_STEPS"),
            40,
            name="DELIVERY_MAX_STEPS",
        )
        self.delivery_max_tokens: int = _parse_int(
            os.getenv("DELIVERY_MAX_TOKENS"),
            400_000,
            name="DELIVERY_MAX_TOKENS",
        )
        self.delivery_run_record_dir: str = (
            os.getenv("DELIVERY_RUN_RECORD_DIR", "data/delivery-runs")
            or "data/delivery-runs"
        ).strip()
```

Add these checks to `Settings.validate()`.

```python
        if self.delivery_topology not in {"single", "verified"}:
            raise ValueError(
                "DELIVERY_TOPOLOGY 只允许 single 或 verified，"
                f"得到 {self.delivery_topology!r}"
            )
        if self.delivery_max_steps <= 0:
            raise ValueError(
                f"DELIVERY_MAX_STEPS 必须为正整数，得到 {self.delivery_max_steps}"
            )
        if self.delivery_max_tokens <= 0:
            raise ValueError(
                f"DELIVERY_MAX_TOKENS 必须为正整数，得到 {self.delivery_max_tokens}"
            )
        if not self.delivery_run_record_dir:
            raise ValueError("DELIVERY_RUN_RECORD_DIR 不能为空")
```

Add the matching settings to `.env.template`.

```dotenv
# ── 受治理仓库交付 Agent（ADR-031）────────────────────────────────────
# single = 单 Implementer；verified 只给预注册 S1 实验，不是默认产品路径。
DELIVERY_TOPOLOGY=single
DELIVERY_MAX_STEPS=40
DELIVERY_MAX_TOKENS=400000
DELIVERY_RUN_RECORD_DIR=data/delivery-runs
```

- [ ] **Step 4: Write ADR-031 and align the public boundary**

Create `docs/adr/adr-031-governed-repository-delivery-agent.md` with these exact decisions:

```markdown
# ADR-031: 受治理的仓库交付 Agent

日期：2026-10-27
状态：已接受，待实施
关联：ADR-021、ADR-023、ADR-027、ADR-030

## 决策

1. 产品入口收敛为 `DeliveryRequest -> DeliveryRun`。
2. 默认拓扑是单 Implementer；只读 Verifier 是预注册实验 S1，不是默认路径。
3. Implementer 可读写工作区并运行固定验证命令，不能访问 GitHub、审批或合并。
4. Verifier 只能读文件、搜索和运行固定验证命令，不能写文件、审批或合并。
5. 所有远端写仍经 `ChangePipeline` 的 bundle、审批和审计顺序。
6. PR 创建后直接登记 `MergeRecord`，合并继续由现有 worker、审批和 SHA 锁完成。
7. CI 红只转人工，v1 不做 CI 自动修复。
8. 不新增队列、dashboard、RAG、MCP、多租户或数据库表。

## 后果

- “开发辅助 Agent”不再是产品否认句；它是本 ADR 定义的受治理交付负载。
- 对外只能宣称“受治理的仓库交付 Agent”，S1 只能宣称“预注册实验能力”。
- 默认部署仍为 dry-run 和单 Agent；真实推送必须显式开启并人工审批。
```

In `README.md`, replace the blanket claim that development assistants are not a product with a scoped statement:

```markdown
> - **开发辅助类 Agent**——这里指受治理的仓库交付 Agent。它默认是单 Implementer，
>   负责改代码、验证、产出可审批的 PR；只读 Verifier 是预注册实验，不是默认拓扑。
```

Add a short “受治理仓库交付 Agent” subsection under the existing write-side section. It must state:

```markdown
产品入口是 `DeliveryRequest -> DeliveryRun`。S0 默认单 Implementer；所有推送仍经过
bundle hash 与人工审批，PR 创建后进入现有合并通道。Verifier 是预注册实验 S1，
默认关闭；CI 失败转人工，不自动修复。
```

In `CONTEXT.md`, add these terms under a new `## 仓库交付` heading:

```markdown
**交付请求 (DeliveryRequest)**:
一次受治理仓库交付的冻结输入，包含 repo、base SHA、分支、目标与来源引用。
_Avoid_: prompt、任务消息

**交付运行 (DeliveryRun)**:
从实现到 PR/CI/合并证据的一次不可变运行记录。`run_id` 标识执行，`trace_id` 标识业务来源。
_Avoid_: 日志、Trello 卡片

**实现体 (Implementer)**:
唯一默认可写工作区的 LLM 执行体。它不能推送、审批或合并。
_Avoid_: 全权 Agent、自动合并器

**复核体 (Verifier)**:
只在 S1 实验中出现的只读执行体。它的 `approved` 只表示可进入人工推送审批。
_Avoid_: 判决者、第二 Implementer
```

In `delivery/定位与减法清单.md`, replace the sentence `开发辅助（改 bug、审 PR）是任务载体，不是产品定位。` with:

```markdown
仓库交付 Agent 是当前产品负载：默认单 Implementer，所有写操作受 bundle、审批与合并状态机约束。
PR 审查是第一个成熟负载；只读 Verifier 是预注册实验 S1，默认关闭。
```

- [ ] **Step 5: Run the documentation and configuration checks**

Run:

```powershell
uv run pytest tests/unit/test_config.py tests/unit/test_config_consistency.py -q
rg -n "不是要做成产品|开发辅助（改 bug、审 PR）是任务载体" README.md delivery CONTEXT.md
```

Expected: pytest passes; `rg` prints no matches.

- [ ] **Step 6: Commit the boundary change**

```powershell
git add docs/adr/adr-031-governed-repository-delivery-agent.md README.md CONTEXT.md delivery/定位与减法清单.md .env.template app/config.py tests/unit/test_config.py
git commit -m "docs: define governed delivery product boundary"
```

---

### Task 2: Add Delivery Contracts and the Outer State Machine

**Files:**
- Create: `apps/code_review_pipeline/delivery/__init__.py`
- Create: `apps/code_review_pipeline/delivery/models.py`
- Create: `apps/code_review_pipeline/delivery/state.py`
- Test: `tests/unit/test_delivery_models.py`
- Test: `tests/unit/test_delivery_state.py`

**Interfaces:**
- Consumes: `ChangeBundle` from `apps.code_review_pipeline.change.bundle`, `MergeRecord` from `apps.code_review_pipeline.merge_store`, and `PullRequestRef` from `apps.code_review_pipeline.change.refs`.
- Produces: all frozen contracts named in the spec, `SCHEMA_VERSION = 1`, `DeliveryRun.to_dict()`, `ALLOWED_TRANSITIONS`, `assert_transition(current, target)`.

- [ ] **Step 1: Write the failing model tests**

Create `tests/unit/test_delivery_models.py`.

```python
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    AgentAttempt,
    AgentStep,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    FailureCode,
    FailureInfo,
)


def _request() -> DeliveryRequest:
    return DeliveryRequest(
        trace_id="trace-1",
        repo="owner/name",
        base_sha="a" * 40,
        base_branch="main",
        branch="moa/fix-1",
        goal="fix add",
        title="fix: correct add",
        body="body",
        origin="manual",
        source_ref="",
    )


def _usage() -> DeliveryUsage:
    return DeliveryUsage(
        llm_calls=2,
        prompt_tokens=30,
        completion_tokens=10,
        cost_usd=0.01,
        llm_latency_ms=12.5,
    )


def test_request_is_immutable_and_uses_the_schema_version() -> None:
    request = _request()
    with pytest.raises(FrozenInstanceError):
        request.branch = "other"  # type: ignore[misc]
    run = DeliveryRun(
        schema_version=SCHEMA_VERSION,
        run_id="run-1",
        trace_id=request.trace_id,
        request=request,
        topology="single",
        state=DeliveryRunState.PREPARING,
        attempts=(),
        verification=None,
        verifier_verdicts=(),
        bundle=None,
        push=None,
        merge=None,
        usage=DeliveryUsage(),
        failure=None,
        started_at=datetime(2026, 10, 27, tzinfo=UTC),
        updated_at=datetime(2026, 10, 27, tzinfo=UTC),
    )
    payload = run.to_dict()
    assert payload["schema_version"] == 1
    assert payload["state"] == "preparing"
    assert payload["request"]["repo"] == "owner/name"
    assert set(payload) == {
        "schema_version",
        "run_id",
        "trace_id",
        "request",
        "topology",
        "state",
        "attempts",
        "verification",
        "verifier_verdicts",
        "bundle",
        "push",
        "merge",
        "usage",
        "failure",
        "started_at",
        "updated_at",
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"repo": "name-only"}, "owner/name"),
        ({"trace_id": " "}, "trace_id"),
        ({"branch": ""}, "branch"),
        ({"base_sha": ""}, "base_sha"),
        ({"goal": ""}, "goal"),
    ],
)
def test_request_rejects_invalid_identity(kwargs: dict[str, str], message: str) -> None:
    values = {
        "trace_id": "trace-1",
        "repo": "owner/name",
        "base_sha": "a" * 40,
        "base_branch": "main",
        "branch": "moa/fix-1",
        "goal": "fix add",
        "title": "fix",
        "body": "",
        "origin": "manual",
        "source_ref": "",
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        DeliveryRequest(**values)  # type: ignore[arg-type]


def test_policy_defaults_and_fixed_revision_budget() -> None:
    policy = DeliveryPolicy(verify_command=("python", "-m", "pytest", "-q"))
    assert policy.topology == "single"
    assert policy.max_revisions == 1
    assert policy.max_steps == 40
    assert policy.max_tokens == 400_000
    with pytest.raises(ValueError, match="max_revisions"):
        DeliveryPolicy(verify_command=("pytest",), max_revisions=2)


def test_attempt_usage_and_failure_serialize_without_losing_evidence() -> None:
    attempt = AgentAttempt(
        role="implementer",
        status="completed",
        summary="fixed",
        stop_reason="model finished",
        usage=_usage(),
        steps=(
            AgentStep(
                index=1,
                action="call_tool",
                tool="write_file",
                arguments={"path": "app.py"},
                observation="written",
                outcome="ok",
                elapsed_ms=1.5,
            ),
        ),
    )
    payload = attempt.to_dict()
    assert payload["usage"]["llm_calls"] == 2
    assert payload["steps"][0]["tool"] == "write_file"
    assert FailureInfo(FailureCode.VERIFY_FAILED, "red").to_dict() == {
        "code": "verify_failed",
        "message": "red",
    }
```

- [ ] **Step 2: Write the failing state tests**

Create `tests/unit/test_delivery_state.py`.

```python
from __future__ import annotations

import pytest

from apps.code_review_pipeline.delivery.models import DeliveryRunState
from apps.code_review_pipeline.delivery.state import (
    ALLOWED_TRANSITIONS,
    InvalidDeliveryTransition,
    assert_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeliveryRunState.PREPARING, DeliveryRunState.IMPLEMENTING),
        (DeliveryRunState.IMPLEMENTING, DeliveryRunState.CHECKING),
        (DeliveryRunState.CHECKING, DeliveryRunState.READY_FOR_PUSH_APPROVAL),
        (DeliveryRunState.CHECKING, DeliveryRunState.REVISING),
        (DeliveryRunState.VERIFIER_REVIEW, DeliveryRunState.BUDGET_EXHAUSTED),
        (DeliveryRunState.REVISING, DeliveryRunState.BUDGET_EXHAUSTED),
        (DeliveryRunState.READY_FOR_PUSH_APPROVAL, DeliveryRunState.PUSHING),
        (DeliveryRunState.PUSHING, DeliveryRunState.WAITING_CI),
        (DeliveryRunState.WAITING_CI, DeliveryRunState.AWAITING_MERGE_APPROVAL),
        (DeliveryRunState.AWAITING_MERGE_APPROVAL, DeliveryRunState.MERGING),
        (DeliveryRunState.MERGING, DeliveryRunState.MERGED),
    ],
)
def test_legal_transitions(
    current: DeliveryRunState, target: DeliveryRunState
) -> None:
    assert_transition(current, target)
    assert target in ALLOWED_TRANSITIONS[current]


def test_terminal_states_do_not_transition() -> None:
    for state in (
        DeliveryRunState.MERGED,
        DeliveryRunState.REJECTED,
        DeliveryRunState.NEEDS_HUMAN,
        DeliveryRunState.BUDGET_EXHAUSTED,
        DeliveryRunState.FAILED,
    ):
        assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_illegal_transition_names_both_states() -> None:
    with pytest.raises(InvalidDeliveryTransition) as caught:
        assert_transition(
            DeliveryRunState.PREPARING,
            DeliveryRunState.PUSHING,
        )
    assert "preparing" in str(caught.value)
    assert "pushing" in str(caught.value)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run:

```powershell
uv run pytest tests/unit/test_delivery_models.py tests/unit/test_delivery_state.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'apps.code_review_pipeline.delivery'`.

- [ ] **Step 4: Implement the immutable contracts**

Create `apps/code_review_pipeline/delivery/models.py` with frozen dataclasses and the following serialization behavior.

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.merge_store import MergeRecord

SCHEMA_VERSION = 1

DeliveryOrigin = Literal["manual", "issue", "ci_failure"]
DeliveryTopology = Literal["single", "verified"]
AgentRole = Literal["implementer", "verifier"]
AgentStatus = Literal["completed", "step_limit", "budget_exhausted", "error"]


@dataclass(frozen=True)
class DeliveryRequest:
    trace_id: str
    repo: str
    base_sha: str
    base_branch: str
    branch: str
    goal: str
    title: str
    body: str
    origin: DeliveryOrigin = "manual"
    source_ref: str = ""

    def __post_init__(self) -> None:
        owner, separator, name = self.repo.partition("/")
        if not separator or not owner.strip() or not name.strip() or "/" in name:
            raise ValueError(f"repo 必须是 owner/name 形式: {self.repo!r}")
        for name, value in (
            ("trace_id", self.trace_id),
            ("base_sha", self.base_sha),
            ("base_branch", self.base_branch),
            ("branch", self.branch),
            ("goal", self.goal),
            ("title", self.title),
        ):
            if not value.strip():
                raise ValueError(f"{name} 不能为空")
        if self.origin not in {"manual", "issue", "ci_failure"}:
            raise ValueError(f"origin 非法: {self.origin!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "repo": self.repo,
            "base_sha": self.base_sha,
            "base_branch": self.base_branch,
            "branch": self.branch,
            "goal": self.goal,
            "title": self.title,
            "body": self.body,
            "origin": self.origin,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class DeliveryPolicy:
    verify_command: tuple[str, ...]
    topology: DeliveryTopology = "single"
    max_revisions: int = 1
    max_steps: int = 40
    max_tokens: int = 400_000

    def __post_init__(self) -> None:
        if not self.verify_command:
            raise ValueError("verify_command 不能为空")
        if self.topology not in {"single", "verified"}:
            raise ValueError(f"topology 非法: {self.topology!r}")
        if self.max_revisions != 1:
            raise ValueError("max_revisions 固定为 1")
        if self.max_steps <= 0:
            raise ValueError("max_steps 必须为正整数")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens 必须为正整数")


@dataclass(frozen=True)
class AgentStep:
    index: int
    action: Literal["call_tool", "finish"]
    tool: str
    arguments: dict[str, Any]
    observation: str
    outcome: Literal["ok", "rejected", "failed", ""]
    elapsed_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "observation": self.observation,
            "outcome": self.outcome,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True)
class DeliveryUsage:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "DeliveryUsage") -> "DeliveryUsage":
        return DeliveryUsage(
            llm_calls=self.llm_calls + other.llm_calls,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            llm_latency_ms=self.llm_latency_ms + other.llm_latency_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "llm_latency_ms": self.llm_latency_ms,
        }


@dataclass(frozen=True)
class AgentAttempt:
    role: AgentRole
    status: AgentStatus
    summary: str
    stop_reason: str
    usage: DeliveryUsage
    steps: tuple[AgentStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "status": self.status,
            "summary": self.summary,
            "stop_reason": self.stop_reason,
            "usage": self.usage.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class EvidenceRef:
    kind: Literal["file", "command", "diff"]
    ref: str
    observation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "observation": self.observation,
        }


@dataclass(frozen=True)
class VerifierVerdict:
    decision: Literal["approved", "revise"]
    reason: str
    instructions: str
    evidence: tuple[EvidenceRef, ...]
    reviewed_bundle_hash: str
    reviewed_patch_hash: str
    attempt: AgentAttempt

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "instructions": self.instructions,
            "evidence": [item.to_dict() for item in self.evidence],
            "reviewed_bundle_hash": self.reviewed_bundle_hash,
            "reviewed_patch_hash": self.reviewed_patch_hash,
            "attempt": self.attempt.to_dict(),
        }


@dataclass(frozen=True)
class PushOutcome:
    branch: str
    commit_sha: str
    pull_request: PullRequestRef

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "pull_request": {
                "number": self.pull_request.number,
                "url": self.pull_request.url,
                "head_sha": self.pull_request.head_sha,
            },
        }


@dataclass(frozen=True)
class VerificationEvidence:
    command: tuple[str, ...]
    exit_code: int
    output_hash: str
    output_excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "output_hash": self.output_hash,
            "output_excerpt": self.output_excerpt,
        }


class DeliveryRunState(str, Enum):
    PREPARING = "preparing"
    IMPLEMENTING = "implementing"
    CHECKING = "checking"
    VERIFIER_REVIEW = "verifier_review"
    REVISING = "revising"
    READY_FOR_PUSH_APPROVAL = "ready_for_push_approval"
    PUSHING = "pushing"
    WAITING_CI = "waiting_ci"
    AWAITING_MERGE_APPROVAL = "awaiting_merge_approval"
    MERGING = "merging"
    MERGED = "merged"
    REJECTED = "rejected"
    NEEDS_HUMAN = "needs_human"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


class FailureCode(str, Enum):
    NO_CHANGES = "no_changes"
    VERIFY_FAILED = "verify_failed"
    VERIFIER_INVALID = "verifier_invalid"
    VERIFIER_REVISE_EXHAUSTED = "verifier_revise_exhausted"
    BUNDLE_CHANGED = "bundle_changed"
    APPROVAL_REJECTED = "approval_rejected"
    PUSH_FAILED = "push_failed"
    CI_FAILED = "ci_failed"
    MERGE_SHA_MISMATCH = "merge_sha_mismatch"
    MERGE_NOT_MERGEABLE = "merge_not_mergeable"
    INFRA_ERROR = "infra_error"


@dataclass(frozen=True)
class FailureInfo:
    code: FailureCode
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "message": self.message}


def _merge_to_dict(record: MergeRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "repo": record.repo,
        "pr_number": record.pr_number,
        "head_sha": record.head_sha,
        "state": record.state,
        "ci_state": record.ci_state,
        "ci_failing": [dict(item) for item in record.ci_failing],
        "card_message_id": record.card_message_id,
        "approver": record.approver,
        "merged_sha": record.merged_sha,
        "failure_reason": record.failure_reason,
        "transitions": [dict(item) for item in record.transitions],
    }


@dataclass(frozen=True)
class DeliveryRun:
    schema_version: int
    run_id: str
    trace_id: str
    request: DeliveryRequest
    topology: DeliveryTopology
    state: DeliveryRunState
    attempts: tuple[AgentAttempt, ...]
    verification: VerificationEvidence | None
    verifier_verdicts: tuple[VerifierVerdict, ...]
    bundle: ChangeBundle | None
    push: PushOutcome | None
    merge: MergeRecord | None
    usage: DeliveryUsage
    failure: FailureInfo | None
    started_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "request": self.request.to_dict(),
            "topology": self.topology,
            "state": self.state.value,
            "attempts": [item.to_dict() for item in self.attempts],
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "verifier_verdicts": [
                item.to_dict() for item in self.verifier_verdicts
            ],
            "bundle": self.bundle.to_dict() if self.bundle else None,
            "push": self.push.to_dict() if self.push else None,
            "merge": _merge_to_dict(self.merge),
            "usage": self.usage.to_dict(),
            "failure": self.failure.to_dict() if self.failure else None,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
```

Create `apps/code_review_pipeline/delivery/state.py`.

```python
from __future__ import annotations

from apps.code_review_pipeline.delivery.models import DeliveryRunState

ALLOWED_TRANSITIONS: dict[DeliveryRunState, frozenset[DeliveryRunState]] = {
    DeliveryRunState.PREPARING: frozenset(
        {DeliveryRunState.IMPLEMENTING, DeliveryRunState.NEEDS_HUMAN}
    ),
    DeliveryRunState.IMPLEMENTING: frozenset(
        {
            DeliveryRunState.CHECKING,
            DeliveryRunState.BUDGET_EXHAUSTED,
            DeliveryRunState.FAILED,
        }
    ),
    DeliveryRunState.CHECKING: frozenset(
        {
            DeliveryRunState.VERIFIER_REVIEW,
            DeliveryRunState.READY_FOR_PUSH_APPROVAL,
            DeliveryRunState.REVISING,
            DeliveryRunState.NEEDS_HUMAN,
            DeliveryRunState.BUDGET_EXHAUSTED,
        }
    ),
    DeliveryRunState.VERIFIER_REVIEW: frozenset(
        {
            DeliveryRunState.READY_FOR_PUSH_APPROVAL,
            DeliveryRunState.REVISING,
            DeliveryRunState.NEEDS_HUMAN,
            DeliveryRunState.BUDGET_EXHAUSTED,
        }
    ),
    DeliveryRunState.REVISING: frozenset(
        {
            DeliveryRunState.CHECKING,
            DeliveryRunState.NEEDS_HUMAN,
            DeliveryRunState.BUDGET_EXHAUSTED,
        }
    ),
    DeliveryRunState.READY_FOR_PUSH_APPROVAL: frozenset(
        {DeliveryRunState.PUSHING, DeliveryRunState.REJECTED}
    ),
    DeliveryRunState.PUSHING: frozenset(
        {DeliveryRunState.WAITING_CI, DeliveryRunState.FAILED}
    ),
    DeliveryRunState.WAITING_CI: frozenset(
        {
            DeliveryRunState.AWAITING_MERGE_APPROVAL,
            DeliveryRunState.NEEDS_HUMAN,
        }
    ),
    DeliveryRunState.AWAITING_MERGE_APPROVAL: frozenset(
        {
            DeliveryRunState.MERGING,
            DeliveryRunState.REJECTED,
            DeliveryRunState.NEEDS_HUMAN,
        }
    ),
    DeliveryRunState.MERGING: frozenset(
        {DeliveryRunState.MERGED, DeliveryRunState.NEEDS_HUMAN}
    ),
    DeliveryRunState.MERGED: frozenset(),
    DeliveryRunState.REJECTED: frozenset(),
    DeliveryRunState.NEEDS_HUMAN: frozenset(),
    DeliveryRunState.BUDGET_EXHAUSTED: frozenset(),
    DeliveryRunState.FAILED: frozenset(),
}


class InvalidDeliveryTransition(Exception):
    def __init__(
        self,
        current: DeliveryRunState,
        target: DeliveryRunState,
    ) -> None:
        allowed = ", ".join(
            sorted(item.value for item in ALLOWED_TRANSITIONS[current])
        )
        super().__init__(
            f"delivery state {current.value!r} does not allow {target.value!r}"
            + (f"; allowed: {allowed}" if allowed else "; state is terminal")
        )


def assert_transition(
    current: DeliveryRunState,
    target: DeliveryRunState,
) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidDeliveryTransition(current, target)
```

Create `apps/code_review_pipeline/delivery/__init__.py` exporting the contracts and state helpers used by later tasks.

```python
from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    AgentAttempt,
    AgentStep,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    EvidenceRef,
    FailureCode,
    FailureInfo,
    PushOutcome,
    VerificationEvidence,
    VerifierVerdict,
)
from apps.code_review_pipeline.delivery.state import (
    ALLOWED_TRANSITIONS,
    InvalidDeliveryTransition,
    assert_transition,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "SCHEMA_VERSION",
    "AgentAttempt",
    "AgentStep",
    "DeliveryPolicy",
    "DeliveryRequest",
    "DeliveryRun",
    "DeliveryRunState",
    "DeliveryUsage",
    "EvidenceRef",
    "FailureCode",
    "FailureInfo",
    "InvalidDeliveryTransition",
    "PushOutcome",
    "VerificationEvidence",
    "VerifierVerdict",
    "assert_transition",
]
```

- [ ] **Step 5: Add the neutral `PullRequestRef` contract required by the model**

Create `apps/code_review_pipeline/change/refs.py`.

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PullRequestRef:
    number: int
    url: str
    head_sha: str
```

In `apps/code_review_pipeline/routing/github_client.py`, import this type.

```python
from apps.code_review_pipeline.change.refs import PullRequestRef
```

Do not change `create_pull_request` yet; Task 3 updates its return value and tests together.

- [ ] **Step 6: Run the contract tests**

Run:

```powershell
uv run pytest tests/unit/test_delivery_models.py tests/unit/test_delivery_state.py -q
uv run ruff check apps/code_review_pipeline/delivery tests/unit/test_delivery_models.py tests/unit/test_delivery_state.py
```

Expected: all tests pass; Ruff reports `All checks passed!`.

- [ ] **Step 7: Commit the contracts**

```powershell
git add apps/code_review_pipeline/change/refs.py apps/code_review_pipeline/delivery/__init__.py apps/code_review_pipeline/delivery/models.py apps/code_review_pipeline/delivery/state.py apps/code_review_pipeline/routing/github_client.py tests/unit/test_delivery_models.py tests/unit/test_delivery_state.py
git commit -m "feat: add governed delivery contracts"
```

---

### Task 3: Split Change Preparation from Publishing and Return a PR Identity

**Files:**
- Modify: `apps/code_review_pipeline/change/pipeline.py:35-365`
- Modify: `apps/code_review_pipeline/change/__init__.py`
- Modify: `apps/code_review_pipeline/routing/github_client.py:448-474`
- Modify: `tests/unit/test_change_pipeline.py`
- Modify: `tests/unit/test_github_client_trust_env.py`

**Interfaces:**
- Consumes: current verified `build_change_bundle`, `ChangeApproval`, `ApprovalRecorder`, and GitHub write primitives.
- Produces: `ChangePipeline.prepare_bundle(request) -> ChangeResult`, `ChangePipeline.publish_bundle(bundle, approval) -> ChangeResult`, extended `ChangeResult.pull_request: PullRequestRef | None`, `ChangeResult.commit_sha: str`, `ChangeBundleChanged`, and `GitHubClient.create_pull_request(...) -> PullRequestRef`.

- [ ] **Step 1: Update test doubles to return `PullRequestRef`**

At the top of `tests/unit/test_change_pipeline.py`, import `PullRequestRef`.

```python
from apps.code_review_pipeline.change.pipeline import ChangeBundleChanged
from apps.code_review_pipeline.change.refs import PullRequestRef
```

Add this helper.

```python
def _pr_ref(number: int = 1) -> PullRequestRef:
    return PullRequestRef(
        number=number,
        url=f"https://example.com/pr/{number}",
        head_sha="b" * 40,
    )
```

Replace every fake `create_pull_request` return value `"https://example.com/pr/1"` with `_pr_ref()`.

- [ ] **Step 2: Add failing tests for prepare/publish separation**

Append these tests to `tests/unit/test_change_pipeline.py`.

```python
def test_prepare_bundle_has_no_remote_side_effect(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    calls: list[str] = []

    class ExplodingGitHub:
        def __getattr__(self, name: str) -> object:
            calls.append(name)
            raise AssertionError(f"prepare must not touch GitHub: {name}")

    pipeline = ChangePipeline(
        github=ExplodingGitHub(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
        push_enabled=True,
    )

    import asyncio

    prepared = asyncio.run(pipeline.prepare_bundle(_request()))

    assert prepared.status is ChangeStatus.VERIFIED
    assert prepared.bundle is not None
    assert calls == []


def test_publish_bundle_does_not_run_verification_again(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    verify_calls = 0

    class RecordingGitHub:
        async def commit_exists(self, repo, sha):
            return True

        async def create_ref(self, repo, branch, base_sha):
            return {"ref": branch}

        async def create_commit(self, repo, **kwargs):
            return "b" * 40

        async def create_pull_request(self, repo, **kwargs):
            return _pr_ref()

    async def record_approval(**kwargs):
        return None

    pipeline = ChangePipeline(
        github=RecordingGitHub(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
        push_enabled=True,
        approval_recorder=record_approval,
    )

    async def counted_verify():
        nonlocal verify_calls
        verify_calls += 1
        return await ChangePipeline._verify(pipeline)

    pipeline._verify = counted_verify  # type: ignore[method-assign]

    import asyncio

    prepared = asyncio.run(pipeline.prepare_bundle(_request()))
    assert prepared.bundle is not None
    published = asyncio.run(
        pipeline.publish_bundle(
            prepared.bundle,
            ChangeApproval(
                approver="alice",
                trace_id="trace-1",
                bundle_hash=prepared.bundle.bundle_hash,
            ),
        )
    )

    assert verify_calls == 1
    assert published.status is ChangeStatus.PUSHED
    assert published.commit_sha == "b" * 40
    assert published.pull_request == _pr_ref()


def test_publish_rejects_a_bundle_changed_after_prepare(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    class NoRemoteWriteGitHub:
        async def commit_exists(self, repo, sha):
            return True

        async def create_ref(self, *args, **kwargs):
            raise AssertionError("changed bundle must not create a ref")

    pipeline = ChangePipeline(
        github=NoRemoteWriteGitHub(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
        push_enabled=True,
    )

    import asyncio

    prepared = asyncio.run(pipeline.prepare_bundle(_request()))
    assert prepared.bundle is not None
    (repo / "app.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")

    with pytest.raises(ChangeBundleChanged, match="changed after bundle"):
        asyncio.run(
            pipeline.publish_bundle(
                prepared.bundle,
                ChangeApproval(
                    approver="alice",
                    trace_id="trace-1",
                    bundle_hash=prepared.bundle.bundle_hash,
                ),
            )
        )
```

- [ ] **Step 3: Run the focused tests to verify they fail**

Run: `uv run pytest tests/unit/test_change_pipeline.py -q`

Expected: FAIL because `prepare_bundle`, `publish_bundle`, `ChangeResult.commit_sha`, and `ChangeResult.pull_request` do not exist; fakes returning strings fail the new `PullRequestRef` expectation.

- [ ] **Step 4: Implement the validators and split pipeline**

Add this domain exception beside `ChangeStatus`.

```python
class ChangeBundleChanged(RuntimeError):
    """The workspace no longer matches the bundle a human approved."""
```

Extend `ChangeResult`.

```python
@dataclass(frozen=True)
class ChangeResult:
    status: ChangeStatus
    bundle: ChangeBundle | None = None
    message: str = ""
    pr_url: str = ""
    commit_sha: str = ""
    pull_request: PullRequestRef | None = None
    audit: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {ChangeStatus.VERIFIED, ChangeStatus.PUSHED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "pr_url": self.pr_url,
            "commit_sha": self.commit_sha,
            "pull_request": (
                {
                    "number": self.pull_request.number,
                    "url": self.pull_request.url,
                    "head_sha": self.pull_request.head_sha,
                }
                if self.pull_request
                else None
            ),
            "bundle": self.bundle.to_dict() if self.bundle else None,
            "audit": dict(self.audit),
        }
```

Add this import at the top of `pipeline.py`.

```python
from apps.code_review_pipeline.change.refs import PullRequestRef
```

Replace `run` with a facade plus two explicit phases.

```python
    async def run(
        self,
        request: ChangeRequest,
        *,
        agent: ChangeAgent | None = None,
        approval: ChangeApproval | None = None,
    ) -> ChangeResult:
        if agent is not None:
            await agent.run(workspace=self._workspace, goal=request.goal)
        prepared = await self.prepare_bundle(request)
        if prepared.status is not ChangeStatus.VERIFIED:
            return prepared
        assert prepared.bundle is not None
        return await self.publish_bundle(prepared.bundle, approval)

    async def prepare_bundle(self, request: ChangeRequest) -> ChangeResult:
        if request.repo.strip() != self._repo:
            raise ValueError(
                f"request repo does not match pipeline repo: "
                f"{request.repo!r} != {self._repo!r}"
            )
        if not self._verify_command:
            return ChangeResult(
                status=ChangeStatus.NO_VERIFY_COMMAND,
                message="未配置验证命令；未验证的变更不允许出口（ADR-030 决策 4）",
            )
        if not self._model_name:
            return ChangeResult(
                status=ChangeStatus.MODEL_NOT_CONFIGURED,
                message="未配置变更模型；拒绝静默降级成规则拼装（ADR-030 决策 8）",
            )

        verify = await self._verify()
        branch = _traceable_branch(request.branch, request.trace_id)
        bundle = build_change_bundle(
            workspace=self._workspace,
            base_sha=request.base_sha,
            branch=branch,
            title=request.title,
            body=request.body,
            verify_command=self._verify_command,
            verify_exit_code=verify.returncode,
            verify_output=verify.stdout[-4000:],
            model=self._model_name,
            trace_id=request.trace_id,
        )
        audit = {
            "repo": self._repo,
            "base_sha": request.base_sha,
            "bundle_hash": bundle.bundle_hash,
            "patch_hash": bundle.patch_hash,
            "verify_exit_code": verify.returncode,
            "approver": "",
            "push_enabled": self._push_enabled,
        }
        if verify.returncode != 0:
            return ChangeResult(
                status=ChangeStatus.VERIFY_FAILED,
                bundle=bundle,
                message=f"验证失败（exit={verify.returncode}），不生成可推送的包",
                audit=audit,
            )
        if not bundle.files:
            return ChangeResult(
                status=ChangeStatus.NO_CHANGES,
                bundle=bundle,
                message="工作区没有改动",
                audit=audit,
            )
        return ChangeResult(
            status=ChangeStatus.VERIFIED,
            bundle=bundle,
            message="验证通过，等待人工推送审批",
            audit=audit,
        )

    async def publish_bundle(
        self,
        bundle: ChangeBundle,
        approval: ChangeApproval | None,
    ) -> ChangeResult:
        audit = {
            "repo": self._repo,
            "base_sha": bundle.base_sha,
            "bundle_hash": bundle.bundle_hash,
            "patch_hash": bundle.patch_hash,
            "verify_exit_code": bundle.verify_exit_code,
            "approver": approval.approver if approval is not None else "",
            "push_enabled": self._push_enabled,
        }
        if not bundle.is_verified:
            return ChangeResult(
                status=ChangeStatus.VERIFY_FAILED,
                bundle=bundle,
                message="bundle is not verified or contains no changes",
                audit=audit,
            )
        if not self._push_enabled:
            return ChangeResult(
                status=ChangeStatus.PUSH_DISABLED,
                bundle=bundle,
                message="dry-run：变更包已生成，未写入远端",
                audit=audit,
            )
        if not bundle.trace_id.strip():
            raise ValueError("trace_id is required for a traceable push")
        approval_granted = approval is not None and (
            approval.decision == "approve" and bool(approval.approver.strip())
        )
        if approval is not None and not approval.approver.strip():
            return ChangeResult(
                status=ChangeStatus.NOT_APPROVED,
                bundle=bundle,
                message="approval record requires a non-empty approver",
                audit=audit,
            )
        if approval is not None and approval.trace_id != bundle.trace_id:
            return ChangeResult(
                status=ChangeStatus.NOT_APPROVED,
                bundle=bundle,
                message="approval record does not match the current trace_id",
                audit=audit,
            )
        if approval is not None and approval.bundle_hash != bundle.bundle_hash:
            return ChangeResult(
                status=ChangeStatus.NOT_APPROVED,
                bundle=bundle,
                message="approval record does not match the current bundle hash",
                audit=audit,
            )
        if not approval_granted:
            return ChangeResult(
                status=ChangeStatus.NOT_APPROVED,
                bundle=bundle,
                message="推送需要人工审批（ADR-030 决策 3）",
                audit=audit,
            )

        assert approval is not None
        await self._record_approval(bundle, approval)
        commit_sha, pull_request = await self._push(bundle)
        return ChangeResult(
            status=ChangeStatus.PUSHED,
            bundle=bundle,
            message="已推送并创建 PR",
            pr_url=pull_request.url,
            commit_sha=commit_sha,
            pull_request=pull_request,
            audit=audit,
        )
```

Change `_push` to retain both write identities.

```python
    async def _push(self, bundle: ChangeBundle) -> tuple[str, PullRequestRef]:
        create_ref = getattr(self._github, "create_ref", None)
        create_commit = getattr(self._github, "create_commit", None)
        create_pr = getattr(self._github, "create_pull_request", None)
        if not all((create_ref, create_commit, create_pr)):
            raise NotImplementedError(
                "GitHub 客户端缺少写侧方法；dry-run 之外的操作尚不可用"
            )
        repo = _split_repo(self._repo)
        if not await self._remote_has_commit(repo, bundle.base_sha):
            raise ValueError(
                f"base_sha 不存在于远端仓库 {self._repo}: {bundle.base_sha[:12]}…；"
                "变更必须基于远端 commit，而不是本地临时 HEAD"
            )
        files = self._file_payload(bundle)
        await create_ref(repo, bundle.branch, bundle.base_sha)
        commit_sha = str(
            await create_commit(
                repo,
                base_sha=bundle.base_sha,
                branch=bundle.branch,
                message=(
                    f"{bundle.title}\n\n{bundle.body}\n\n"
                    f"MoA-Trace-Id: {bundle.trace_id}\n"
                    f"MoA-Bundle-Hash: {bundle.bundle_hash}"
                ).strip(),
                files=files,
            )
        )
        pull_request = await create_pr(
            repo,
            branch=bundle.branch,
            base_branch=self._base_branch,
            title=bundle.title,
            body=bundle.body,
        )
        if not isinstance(pull_request, PullRequestRef):
            raise TypeError("create_pull_request must return PullRequestRef")
        if pull_request.head_sha != commit_sha:
            raise RuntimeError(
                "GitHub PR head SHA does not match the created commit: "
                f"{pull_request.head_sha!r} != {commit_sha!r}"
            )
        return commit_sha, pull_request
```

Change the two post-bundle mutation checks in `_file_payload` to raise the
domain exception rather than a generic `ValueError`.

```python
            if change.kind == "deleted":
                if target.exists():
                    raise ChangeBundleChanged(
                        f"file changed after bundle: {change.path} was recreated"
                    )
                payload.append({"path": change.path, "content_b64": None})
                continue
            content = target.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if digest != change.after_hash:
                raise ChangeBundleChanged(
                    f"file changed after bundle: {change.path}"
                )
```

Add `"ChangeBundleChanged"` to `pipeline.py`'s `__all__` and re-export it from
`apps/code_review_pipeline/change/__init__.py`.

Export `PullRequestRef` from `apps/code_review_pipeline/change/__init__.py`.

```python
from apps.code_review_pipeline.change.refs import PullRequestRef
```

Add it to `__all__`.

```python
    "PullRequestRef",
```

- [ ] **Step 5: Make the real GitHub client return `PullRequestRef`**

Replace `create_pull_request`.

```python
    async def create_pull_request(
        self,
        repo: GitHubRepo,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequestRef:
        """Open a PR and return the identity required by the merge channel."""
        resp = await self._client.post(
            f"/repos/{repo.owner}/{repo.name}/pulls",
            json={
                "head": branch,
                "base": base_branch,
                "title": title,
                "body": body,
            },
        )
        resp.raise_for_status()
        payload = dict(resp.json())
        number = int(payload.get("number") or 0)
        url = str(payload.get("html_url") or "")
        head_sha = str(dict(payload.get("head") or {}).get("sha") or "")
        if number <= 0 or not url or not head_sha:
            raise RuntimeError(
                "GitHub create_pull_request response lacks number/url/head.sha"
            )
        return PullRequestRef(number=number, url=url, head_sha=head_sha)
```

Add a `MockTransport` test to `tests/unit/test_github_client_trust_env.py`.

```python
@pytest.mark.asyncio
async def test_create_pull_request_returns_number_url_and_head_sha() -> None:
    import httpx

    from apps.code_review_pipeline.routing.github_client import GitHubRepo

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/name/pulls"
        return httpx.Response(
            201,
            json={
                "number": 17,
                "html_url": "https://github.com/owner/name/pull/17",
                "head": {"sha": "b" * 40},
            },
        )

    client = GitHubClient(token="t")
    await client._client.aclose()  # noqa: SLF001
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        ref = await client.create_pull_request(
            GitHubRepo(owner="owner", name="name"),
            branch="moa/fix",
            base_branch="main",
            title="fix",
            body="body",
        )
    finally:
        await client.aclose()

    assert ref.number == 17
    assert ref.url.endswith("/pull/17")
    assert ref.head_sha == "b" * 40
```

- [ ] **Step 6: Run the focused and regression tests**

Run:

```powershell
uv run pytest tests/unit/test_change_pipeline.py tests/unit/test_github_client_trust_env.py -q
uv run ruff check apps/code_review_pipeline/change apps/code_review_pipeline/routing/github_client.py tests/unit/test_change_pipeline.py tests/unit/test_github_client_trust_env.py
git diff --check
```

Expected: all focused tests pass, Ruff passes, and `git diff --check` exits 0.

- [ ] **Step 7: Commit the pipeline split**

```powershell
git add apps/code_review_pipeline/change/pipeline.py apps/code_review_pipeline/change/__init__.py apps/code_review_pipeline/routing/github_client.py tests/unit/test_change_pipeline.py tests/unit/test_github_client_trust_env.py
git commit -m "feat: split change preparation from publishing"
```

---

### Task 4: Implement Ports and the Deterministic S0 Delivery Flow

**Files:**
- Create: `apps/code_review_pipeline/delivery/ports.py`
- Create: `apps/code_review_pipeline/delivery/flow.py`
- Modify: `apps/code_review_pipeline/delivery/__init__.py`
- Test: `tests/unit/test_delivery_flow.py`

**Interfaces:**
- Consumes: `DeliveryRequest`, `DeliveryPolicy`, `AgentAttempt`, `ChangeBundle`, `ChangeApproval`, `PushOutcome`, `DeliveryRun` contracts from Task 2; `ChangePipeline.prepare_bundle` and `ChangePipeline.publish_bundle` semantics from Task 3.
- Produces: `ChangePreparation`, `PreparationStatus`, `ImplementerPort`, `ChangePreparerPort`, `PushApprovalPort`, `ChangePublisher`, `RunRecorderPort`, `DeliveryAuditPort`, and `DeliveryFlow.run(request) -> DeliveryRun`.

- [ ] **Step 1: Write the failing flow tests**

Create `tests/unit/test_delivery_flow.py`.

```python
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.pipeline import ChangeApproval
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.delivery.flow import DeliveryFlow
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    AgentStep,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    FailureCode,
    FailureInfo,
    PushOutcome,
    VerificationEvidence,
)
from apps.code_review_pipeline.delivery.ports import (
    BundleChanged,
    ChangePreparation,
    PreparationStatus,
)


def _request() -> DeliveryRequest:
    return DeliveryRequest(
        trace_id="trace-1",
        repo="owner/name",
        base_sha="a" * 40,
        base_branch="main",
        branch="moa/fix-1",
        goal="fix add",
        title="fix: correct add",
        body="body",
    )


def _attempt(*, status: str = "completed", summary: str = "changed app.py") -> AgentAttempt:
    return AgentAttempt(
        role="implementer",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        stop_reason="finished",
        usage=DeliveryUsage(
            llm_calls=1,
            prompt_tokens=100,
            completion_tokens=20,
            cost_usd=0.001,
            llm_latency_ms=10.0,
        ),
        steps=(
            AgentStep(
                index=1,
                action="call_tool",
                tool="write_file",
                arguments={"path": "app.py"},
                observation="ok",
                outcome="ok",
                elapsed_ms=2.0,
            ),
        ),
    )


def _bundle() -> ChangeBundle:
    patch = "diff --git a/app.py b/app.py\n"
    return ChangeBundle(
        base_sha="a" * 40,
        branch="moa/fix-1-trace-1",
        title="fix: correct add",
        body="body",
        files=(),
        patch=patch,
        patch_hash=hashlib.sha256(patch.encode()).hexdigest(),
        verify_command=("python", "-m", "pytest", "-q"),
        verify_exit_code=0,
        verify_output="1 passed",
        model="test-model",
        trace_id="trace-1",
    )


def _verification() -> VerificationEvidence:
    return VerificationEvidence(
        command=("python", "-m", "pytest", "-q"),
        exit_code=0,
        output_hash=hashlib.sha256(b"1 passed").hexdigest(),
        output_excerpt="1 passed",
    )


class FakeImplementer:
    def __init__(self, attempts: list[AgentAttempt]) -> None:
        self._attempts = list(attempts)
        self.feedback: list[str] = []

    async def run(
        self,
        *,
        workspace: Path,
        goal: str,
        feedback: str,
        max_steps: int,
        max_tokens: int,
    ) -> AgentAttempt:
        self.feedback.append(feedback)
        return self._attempts.pop(0)


class FakePreparer:
    def __init__(self, results: list[ChangePreparation]) -> None:
        self._results = list(results)
        self.calls = 0

    async def prepare(
        self,
        *,
        workspace: Path,
        request: DeliveryRequest,
        policy: DeliveryPolicy,
    ) -> ChangePreparation:
        self.calls += 1
        return self._results.pop(0)


class FakeApproval:
    def __init__(self, approval: ChangeApproval | None) -> None:
        self._approval = approval
        self.bundles: list[ChangeBundle] = []

    async def request(
        self,
        *,
        run_id: str,
        bundle: ChangeBundle,
    ) -> ChangeApproval | None:
        self.bundles.append(bundle)
        return self._approval


class FakePublisher:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.calls = 0

    async def publish(
        self,
        *,
        workspace: Path,
        bundle: ChangeBundle,
        approval: ChangeApproval,
    ) -> PushOutcome:
        self.calls += 1
        self._events.append("publish")
        return PushOutcome(
            branch=bundle.branch,
            commit_sha="b" * 40,
            pull_request=PullRequestRef(
                number=9,
                url="https://example.com/pr/9",
                head_sha="b" * 40,
            ),
        )


class FakeRecorder:
    def __init__(self) -> None:
        self.runs: list[DeliveryRun] = []

    async def save(self, run: DeliveryRun) -> None:
        self.runs.append(run)


class FakeAudit:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.transitions: list[tuple[str, str]] = []

    async def record(
        self,
        *,
        run: DeliveryRun,
        previous: DeliveryRunState | None,
        current: DeliveryRunState,
        detail: dict[str, object],
    ) -> None:
        self._events.append(f"audit:{current.value}")
        self.transitions.append(
            (
                previous.value if previous else "",
                current.value,
            )
        )


def _flow(
    tmp_path: Path,
    *,
    implementer: FakeImplementer,
    preparer: FakePreparer,
    approval: FakeApproval | None = None,
    publisher: FakePublisher | None = None,
) -> tuple[DeliveryFlow, FakeRecorder, FakeAudit, FakePublisher]:
    events: list[str] = []
    recorder = FakeRecorder()
    audit = FakeAudit(events)
    publisher = publisher or FakePublisher(events)
    approval = approval or FakeApproval(
        ChangeApproval(
            approver="alice",
            trace_id="trace-1",
            bundle_hash=_bundle().bundle_hash,
        )
    )
    flow = DeliveryFlow(
        workspace=tmp_path,
        policy=DeliveryPolicy(verify_command=("python", "-m", "pytest", "-q")),
        implementer=implementer,
        preparer=preparer,
        approval=approval,
        publisher=publisher,
        recorder=recorder,
        audit=audit,
        clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
        new_run_id=lambda: "run-1",
    )
    return flow, recorder, audit, publisher


@pytest.mark.asyncio
async def test_s0_happy_path_reaches_waiting_ci(tmp_path: Path) -> None:
    bundle = _bundle()
    flow, recorder, audit, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt()]),
        preparer=FakePreparer(
            [
                ChangePreparation(
                    status=PreparationStatus.READY,
                    bundle=bundle,
                    verification=_verification(),
                )
            ]
        ),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.WAITING_CI
    assert run.bundle == bundle
    assert run.push is not None
    assert run.push.pull_request.number == 9
    assert run.usage == _attempt().usage
    assert publisher.calls == 1
    assert run.failure is None
    assert run.run_id == "run-1"
    assert recorder.runs[-1] == run
    assert audit.transitions == [
        ("", "preparing"),
        ("preparing", "implementing"),
        ("implementing", "checking"),
        ("checking", "ready_for_push_approval"),
        ("ready_for_push_approval", "pushing"),
        ("pushing", "waiting_ci"),
    ]


@pytest.mark.asyncio
async def test_verify_failure_uses_one_revision_and_passes_feedback(tmp_path: Path) -> None:
    bundle = _bundle()
    implementer = FakeImplementer([_attempt(), _attempt(summary="fixed second time")])
    preparer = FakePreparer(
        [
            ChangePreparation(
                status=PreparationStatus.VERIFY_FAILED,
                bundle=bundle,
                verification=replace(_verification(), exit_code=1),
                message="tests failed",
            ),
            ChangePreparation(
                status=PreparationStatus.READY,
                bundle=bundle,
                verification=_verification(),
            ),
        ]
    )
    flow, _, _, _ = _flow(tmp_path, implementer=implementer, preparer=preparer)

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.WAITING_CI
    assert len(run.attempts) == 2
    assert implementer.feedback == ["", "tests failed"]
    assert preparer.calls == 2


@pytest.mark.asyncio
async def test_second_verify_failure_stops_without_a_third_implementer(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    implementer = FakeImplementer([_attempt(), _attempt(summary="still broken")])
    failed = ChangePreparation(
        status=PreparationStatus.VERIFY_FAILED,
        bundle=bundle,
        verification=replace(_verification(), exit_code=1),
        message="tests failed",
    )
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=implementer,
        preparer=FakePreparer([failed, failed]),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.NEEDS_HUMAN
    assert run.failure == FailureInfo(FailureCode.VERIFY_FAILED, "tests failed")
    assert len(run.attempts) == 2
    assert publisher.calls == 0


@pytest.mark.asyncio
async def test_human_rejection_stops_before_publish(tmp_path: Path) -> None:
    bundle = _bundle()
    approval = FakeApproval(None)
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt()]),
        preparer=FakePreparer(
            [
                ChangePreparation(
                    status=PreparationStatus.READY,
                    bundle=bundle,
                    verification=_verification(),
                )
            ]
        ),
        approval=approval,
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.REJECTED
    assert run.failure is not None
    assert run.failure.code is FailureCode.APPROVAL_REJECTED
    assert publisher.calls == 0
    assert approval.bundles == [bundle]


@pytest.mark.asyncio
async def test_budget_exhaustion_is_not_a_verification_failure(tmp_path: Path) -> None:
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt(status="budget_exhausted")]),
        preparer=FakePreparer([]),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.BUDGET_EXHAUSTED
    assert run.failure is None
    assert publisher.calls == 0


@pytest.mark.asyncio
async def test_s0_rejects_verified_topology_without_silent_degrade(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="verified topology requires"):
        DeliveryFlow(
            workspace=tmp_path,
            policy=DeliveryPolicy(
                verify_command=("pytest",),
                topology="verified",
            ),
            implementer=FakeImplementer([]),
            preparer=FakePreparer([]),
            approval=FakeApproval(None),
            publisher=FakePublisher([]),
            recorder=FakeRecorder(),
            audit=FakeAudit([]),
            clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
            new_run_id=lambda: "run-1",
        )


@pytest.mark.asyncio
async def test_workspace_change_after_approval_maps_to_bundle_changed(
    tmp_path: Path,
) -> None:
    class ChangedPublisher(FakePublisher):
        async def publish(
            self,
            *,
            workspace: Path,
            bundle: ChangeBundle,
            approval: ChangeApproval,
        ) -> PushOutcome:
            raise BundleChanged("file changed after bundle: app.py")

    bundle = _bundle()
    flow, _, _, publisher = _flow(
        tmp_path,
        implementer=FakeImplementer([_attempt()]),
        preparer=FakePreparer(
            [
                ChangePreparation(
                    status=PreparationStatus.READY,
                    bundle=bundle,
                    verification=_verification(),
                )
            ]
        ),
        publisher=ChangedPublisher([]),
    )

    run = await flow.run(_request())

    assert run.state is DeliveryRunState.FAILED
    assert run.failure == FailureInfo(
        FailureCode.BUNDLE_CHANGED,
        "file changed after bundle: app.py",
    )
    assert publisher.calls == 0
```

- [ ] **Step 2: Run the flow tests to verify they fail**

Run: `uv run pytest tests/unit/test_delivery_flow.py -q`

Expected: FAIL because `delivery.ports` and `delivery.flow` do not exist.

- [ ] **Step 3: Implement the port-only result and protocols**

Create `apps/code_review_pipeline/delivery/ports.py`.

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.pipeline import ChangeApproval
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    PushOutcome,
    VerificationEvidence,
)


class PreparationStatus(str, Enum):
    READY = "ready"
    VERIFY_FAILED = "verify_failed"
    NO_CHANGES = "no_changes"
    INVALID = "invalid"


class BundleChanged(RuntimeError):
    """The concrete publisher detected a workspace/bundle mismatch."""


@dataclass(frozen=True)
class ChangePreparation:
    status: PreparationStatus
    bundle: ChangeBundle | None
    verification: VerificationEvidence | None
    message: str = ""


class ImplementerPort(Protocol):
    async def run(
        self,
        *,
        workspace: Path,
        goal: str,
        feedback: str,
        max_steps: int,
        max_tokens: int,
    ) -> AgentAttempt: ...


class ChangePreparerPort(Protocol):
    async def prepare(
        self,
        *,
        workspace: Path,
        request: DeliveryRequest,
        policy: DeliveryPolicy,
    ) -> ChangePreparation: ...


class PushApprovalPort(Protocol):
    async def request(
        self,
        *,
        run_id: str,
        bundle: ChangeBundle,
    ) -> ChangeApproval | None: ...


class ChangePublisher(Protocol):
    async def publish(
        self,
        *,
        workspace: Path,
        bundle: ChangeBundle,
        approval: ChangeApproval,
    ) -> PushOutcome: ...


class RunRecorderPort(Protocol):
    async def save(self, run: DeliveryRun) -> None: ...


class DeliveryAuditPort(Protocol):
    async def record(
        self,
        *,
        run: DeliveryRun,
        previous: DeliveryRunState | None,
        current: DeliveryRunState,
        detail: dict[str, object],
    ) -> None: ...
```

`ChangePreparerPort` is a deliberate refinement of the approved spec: without it,
`DeliveryFlow` would have to import the concrete `ChangePipeline` or duplicate
`build_change_bundle` and verification. The port keeps the product core dependent
only on protocols and lets the GitHub adapter own the existing pipeline.

- [ ] **Step 4: Implement the deterministic flow**

Create `apps/code_review_pipeline/delivery/flow.py`.

```python
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    FailureCode,
    FailureInfo,
)
from apps.code_review_pipeline.delivery.ports import (
    BundleChanged,
    ChangePreparerPort,
    ChangePublisher,
    DeliveryAuditPort,
    ImplementerPort,
    PreparationStatus,
    PushApprovalPort,
    RunRecorderPort,
)
from apps.code_review_pipeline.delivery.state import assert_transition


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_run_id() -> str:
    return f"dlv-{uuid.uuid4().hex}"


class DeliveryFlow:
    def __init__(
        self,
        *,
        workspace: Path,
        policy: DeliveryPolicy,
        implementer: ImplementerPort,
        preparer: ChangePreparerPort,
        approval: PushApprovalPort,
        publisher: ChangePublisher,
        recorder: RunRecorderPort,
        audit: DeliveryAuditPort,
        clock: Callable[[], datetime] = _utcnow,
        new_run_id: Callable[[], str] = _new_run_id,
    ) -> None:
        if policy.topology != "single":
            raise ValueError(
                "verified topology requires the separately planned S1 Verifier; "
                "S0 refuses to silently degrade"
            )
        self._workspace = Path(workspace).resolve()
        self._policy = policy
        self._implementer = implementer
        self._preparer = preparer
        self._approval = approval
        self._publisher = publisher
        self._recorder = recorder
        self._audit = audit
        self._clock = clock
        self._new_run_id = new_run_id

    async def run(self, request: DeliveryRequest) -> DeliveryRun:
        now = self._clock()
        run = DeliveryRun(
            schema_version=SCHEMA_VERSION,
            run_id=self._new_run_id(),
            trace_id=request.trace_id,
            request=request,
            topology=self._policy.topology,
            state=DeliveryRunState.PREPARING,
            attempts=(),
            verification=None,
            verifier_verdicts=(),
            bundle=None,
            push=None,
            merge=None,
            usage=DeliveryUsage(),
            failure=None,
            started_at=now,
            updated_at=now,
        )
        await self._recorder.save(run)
        await self._audit.record(
            run=run,
            previous=None,
            current=DeliveryRunState.PREPARING,
            detail={"action": "created"},
        )
        run = await self._move(run, DeliveryRunState.IMPLEMENTING, "start")

        feedback = ""
        revisions = 0
        while True:
            remaining_steps, remaining_tokens = self._remaining_budget(run)
            if remaining_steps <= 0 or remaining_tokens <= 0:
                return await self._finish(
                    run,
                    DeliveryRunState.BUDGET_EXHAUSTED,
                    None,
                )
            try:
                attempt = await self._implementer.run(
                    workspace=self._workspace,
                    goal=request.goal,
                    feedback=feedback,
                    max_steps=remaining_steps,
                    max_tokens=remaining_tokens,
                )
            except Exception as exc:
                target = (
                    DeliveryRunState.NEEDS_HUMAN
                    if run.state is DeliveryRunState.REVISING
                    else DeliveryRunState.FAILED
                )
                return await self._finish(
                    run,
                    target,
                    FailureInfo(
                        FailureCode.INFRA_ERROR,
                        f"implementer failed: {type(exc).__name__}: {exc}",
                    ),
                )

            run = replace(
                run,
                attempts=(*run.attempts, attempt),
                usage=run.usage + attempt.usage,
                updated_at=self._clock(),
            )
            await self._recorder.save(run)

            if attempt.status == "budget_exhausted":
                return await self._finish(
                    run,
                    DeliveryRunState.BUDGET_EXHAUSTED,
                    None,
                )
            if run.usage.total_tokens >= self._policy.max_tokens:
                return await self._finish(
                    run,
                    DeliveryRunState.BUDGET_EXHAUSTED,
                    None,
                )
            if attempt.status == "error":
                return await self._finish(
                    run,
                    DeliveryRunState.NEEDS_HUMAN
                    if run.state is DeliveryRunState.REVISING
                    else DeliveryRunState.FAILED,
                    FailureInfo(
                        FailureCode.INFRA_ERROR,
                        attempt.stop_reason or "implementer returned error",
                    ),
                )
            if attempt.status == "step_limit":
                return await self._finish(
                    run,
                    DeliveryRunState.NEEDS_HUMAN
                    if run.state is DeliveryRunState.REVISING
                    else DeliveryRunState.FAILED,
                    FailureInfo(
                        FailureCode.INFRA_ERROR,
                        attempt.stop_reason or "implementer reached step limit",
                    ),
                )

            run = await self._move(run, DeliveryRunState.CHECKING, "verify")
            try:
                preparation = await self._preparer.prepare(
                    workspace=self._workspace,
                    request=request,
                    policy=self._policy,
                )
            except Exception as exc:
                return await self._finish(
                    run,
                    DeliveryRunState.NEEDS_HUMAN,
                    FailureInfo(
                        FailureCode.INFRA_ERROR,
                        f"preparation failed: {type(exc).__name__}: {exc}",
                    ),
                )

            if preparation.verification is not None:
                run = replace(
                    run,
                    verification=preparation.verification,
                    updated_at=self._clock(),
                )
                await self._recorder.save(run)

            if preparation.status is PreparationStatus.READY:
                if preparation.bundle is None or preparation.verification is None:
                    return await self._finish(
                        run,
                        DeliveryRunState.NEEDS_HUMAN,
                        FailureInfo(
                            FailureCode.INFRA_ERROR,
                            "ready preparation lacked bundle or verification evidence",
                        ),
                    )
                run = replace(
                    run,
                    bundle=preparation.bundle,
                    updated_at=self._clock(),
                )
                await self._recorder.save(run)
                break

            retryable = preparation.status in {
                PreparationStatus.VERIFY_FAILED,
                PreparationStatus.NO_CHANGES,
            }
            if retryable and revisions < self._policy.max_revisions:
                revisions += 1
                feedback = preparation.message or preparation.status.value
                run = await self._move(
                    run,
                    DeliveryRunState.REVISING,
                    feedback,
                )
                continue

            code = (
                FailureCode.NO_CHANGES
                if preparation.status is PreparationStatus.NO_CHANGES
                else FailureCode.VERIFY_FAILED
                if preparation.status is PreparationStatus.VERIFY_FAILED
                else FailureCode.INFRA_ERROR
            )
            return await self._finish(
                run,
                DeliveryRunState.NEEDS_HUMAN,
                FailureInfo(code, preparation.message or preparation.status.value),
            )

        assert run.bundle is not None
        run = await self._move(
            run,
            DeliveryRunState.READY_FOR_PUSH_APPROVAL,
            "awaiting_push_approval",
        )
        try:
            approval = await self._approval.request(
                run_id=run.run_id,
                bundle=run.bundle,
            )
        except Exception as exc:
            return await self._finish(
                run,
                DeliveryRunState.REJECTED,
                FailureInfo(
                    FailureCode.INFRA_ERROR,
                    f"push approval failed: {type(exc).__name__}: {exc}",
                ),
            )
        if approval is None:
            return await self._finish(
                run,
                DeliveryRunState.REJECTED,
                FailureInfo(
                    FailureCode.APPROVAL_REJECTED,
                    "human rejected or unavailable for push approval",
                ),
            )

        run = await self._move(run, DeliveryRunState.PUSHING, "approved")
        try:
            push = await self._publisher.publish(
                workspace=self._workspace,
                bundle=run.bundle,
                approval=approval,
            )
        except BundleChanged as exc:
            return await self._finish(
                run,
                DeliveryRunState.FAILED,
                FailureInfo(FailureCode.BUNDLE_CHANGED, str(exc)),
            )
        except Exception as exc:
            return await self._finish(
                run,
                DeliveryRunState.FAILED,
                FailureInfo(
                    FailureCode.PUSH_FAILED,
                    f"{type(exc).__name__}: {exc}",
                ),
            )
        run = replace(run, push=push, updated_at=self._clock())
        await self._recorder.save(run)
        return await self._finish(run, DeliveryRunState.WAITING_CI, None)

    async def _move(
        self,
        run: DeliveryRun,
        target: DeliveryRunState,
        detail: str,
    ) -> DeliveryRun:
        assert_transition(run.state, target)
        previous = run.state
        updated = replace(run, state=target, updated_at=self._clock())
        await self._recorder.save(updated)
        await self._audit.record(
            run=updated,
            previous=previous,
            current=target,
            detail={"action": detail},
        )
        return updated

    def _remaining_budget(self, run: DeliveryRun) -> tuple[int, int]:
        tool_steps = sum(
            1
            for attempt in run.attempts
            for step in attempt.steps
            if step.action == "call_tool"
        )
        return (
            self._policy.max_steps - tool_steps,
            self._policy.max_tokens - run.usage.total_tokens,
        )

    async def _finish(
        self,
        run: DeliveryRun,
        target: DeliveryRunState,
        failure: FailureInfo | None,
    ) -> DeliveryRun:
        updated = await self._move(run, target, failure.code.value if failure else "done")
        if failure is not None:
            updated = replace(updated, failure=failure, updated_at=self._clock())
            await self._recorder.save(updated)
            await self._audit.record(
                run=updated,
                previous=target,
                current=target,
                detail={
                    "action": "failure",
                    "code": failure.code.value,
                    "message": failure.message,
                },
            )
        return updated
```

- [ ] **Step 5: Export the ports and flow**

Modify `apps/code_review_pipeline/delivery/__init__.py` to import and export:

```python
from apps.code_review_pipeline.delivery.flow import DeliveryFlow
from apps.code_review_pipeline.delivery.ports import (
    BundleChanged,
    ChangePreparation,
    ChangePreparerPort,
    ChangePublisher,
    DeliveryAuditPort,
    ImplementerPort,
    PreparationStatus,
    PushApprovalPort,
    RunRecorderPort,
)
```

Add all imported names to `__all__`.

- [ ] **Step 6: Run the flow and contract tests**

Run:

```powershell
uv run pytest tests/unit/test_delivery_models.py tests/unit/test_delivery_state.py tests/unit/test_delivery_flow.py -q
uv run ruff check apps/code_review_pipeline/delivery tests/unit/test_delivery_flow.py
```

Expected: all tests pass and Ruff passes.

- [ ] **Step 7: Commit the flow**

```powershell
git add apps/code_review_pipeline/delivery/__init__.py apps/code_review_pipeline/delivery/ports.py apps/code_review_pipeline/delivery/flow.py tests/unit/test_delivery_flow.py
git commit -m "feat: add deterministic delivery flow"
```

---

### Task 5: Persist Run Records and Write Delivery Transitions to the Existing WAL

**Files:**
- Create: `apps/code_review_pipeline/delivery/run_store.py`
- Create: `apps/code_review_pipeline/delivery/audit.py`
- Modify: `apps/code_review_pipeline/delivery/__init__.py`
- Test: `tests/unit/test_delivery_store.py`

**Interfaces:**
- Consumes: `DeliveryRun.to_dict()` and `DeliveryRunStore.view(run, merge_store)`.
- Produces: `DeliveryRunStore(directory)`, `async save(run)`, `load(run_id)`, `view(run, merge_store)`, and `WalDeliveryAudit.record(...)`.

- [ ] **Step 1: Write the failing persistence and audit tests**

Create `tests/unit/test_delivery_store.py`.

```python
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.code_review_pipeline.change.bundle import ChangeBundle, FileChange
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.delivery.audit import WalDeliveryAudit
from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    AgentAttempt,
    AgentStep,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    EvidenceRef,
    FailureCode,
    FailureInfo,
    PushOutcome,
    VerificationEvidence,
    VerifierVerdict,
)
from apps.code_review_pipeline.delivery.run_store import DeliveryRunStore
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore


def _run(state: DeliveryRunState = DeliveryRunState.READY_FOR_PUSH_APPROVAL) -> DeliveryRun:
    request = DeliveryRequest(
        trace_id="trace-1",
        repo="owner/name",
        base_sha="a" * 40,
        base_branch="main",
        branch="moa/fix",
        goal="fix",
        title="fix",
        body="",
    )
    now = datetime(2026, 10, 27, tzinfo=UTC)
    return DeliveryRun(
        schema_version=SCHEMA_VERSION,
        run_id="run-1",
        trace_id="trace-1",
        request=request,
        topology="single",
        state=state,
        attempts=(),
        verification=None,
        verifier_verdicts=(),
        bundle=None,
        push=None,
        merge=None,
        usage=DeliveryUsage(),
        failure=None,
        started_at=now,
        updated_at=now,
    )


def _full_run() -> DeliveryRun:
    base = _run(DeliveryRunState.NEEDS_HUMAN)
    attempt = AgentAttempt(
        role="implementer",
        status="completed",
        summary="changed app.py",
        stop_reason="model finished",
        usage=DeliveryUsage(
            llm_calls=2,
            prompt_tokens=30,
            completion_tokens=10,
            cost_usd=0.02,
            llm_latency_ms=12.5,
        ),
        steps=(
            AgentStep(
                index=1,
                action="call_tool",
                tool="write_file",
                arguments={"path": "app.py"},
                observation="written",
                outcome="ok",
                elapsed_ms=1.5,
            ),
        ),
    )
    bundle = ChangeBundle(
        base_sha="a" * 40,
        branch="moa/fix-trace-1",
        title="fix",
        body="body",
        files=(
            FileChange(
                path="app.py",
                kind="modified",
                before_hash="1" * 64,
                after_hash="2" * 64,
            ),
        ),
        patch="diff --git a/app.py b/app.py\n",
        patch_hash="3" * 64,
        verify_command=("pytest", "-q"),
        verify_exit_code=0,
        verify_output="1 passed",
        model="test-model",
        trace_id="trace-1",
    )
    verification = VerificationEvidence(
        command=("pytest", "-q"),
        exit_code=0,
        output_hash="4" * 64,
        output_excerpt="1 passed",
    )
    push = PushOutcome(
        branch=bundle.branch,
        commit_sha="b" * 40,
        pull_request=PullRequestRef(
            number=9,
            url="https://example.com/pr/9",
            head_sha="b" * 40,
        ),
    )
    merge = MergeRecord(
        repo="owner/name",
        pr_number=9,
        head_sha="b" * 40,
        state=MergeState.CI_FAILED.value,
        ci_state="failure",
        ci_failing=({"name": "unit", "url": "https://example.com/check/1"},),
        card_message_id="card-1",
        failure_reason="unit failed",
        transitions=(
            {"from": "watching", "to": "ci_failed", "action": "ci_red"},
        ),
    )
    verdict = VerifierVerdict(
        decision="revise",
        reason="missing edge case",
        instructions="add the zero-input test and fix the branch",
        evidence=(
            EvidenceRef(
                kind="file",
                ref="app.py",
                observation="the zero branch is absent",
            ),
        ),
        reviewed_bundle_hash=bundle.bundle_hash,
        reviewed_patch_hash=bundle.patch_hash,
        attempt=AgentAttempt(
            role="verifier",
            status="completed",
            summary="needs revision",
            stop_reason="model finished",
            usage=DeliveryUsage(llm_calls=1, prompt_tokens=20, completion_tokens=5),
            steps=(),
        ),
    )
    return replace(
        base,
        attempts=(attempt,),
        verification=verification,
        verifier_verdicts=(verdict,),
        bundle=bundle,
        push=push,
        merge=merge,
        usage=attempt.usage + verdict.attempt.usage,
        failure=FailureInfo(
            FailureCode.VERIFIER_REVISE_EXHAUSTED,
            "revision budget exhausted",
        ),
    )


@pytest.mark.asyncio
async def test_run_store_round_trips_every_delivery_evidence_field(
    tmp_path: Path,
) -> None:
    store = DeliveryRunStore(tmp_path)
    original = _full_run()

    await store.save(original)

    assert store.load(original.run_id) == original


@pytest.mark.asyncio
async def test_run_store_writes_a_readable_snapshot(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    original = _run()

    await store.save(original)
    loaded = store.load("run-1")

    assert loaded == original
    assert (tmp_path / "run-1.json").is_file()


@pytest.mark.asyncio
async def test_run_store_replaces_atomically_without_partial_json(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    await store.save(_run())
    await store.save(replace(_run(), state=DeliveryRunState.WAITING_CI))

    loaded = store.load("run-1")

    assert loaded.state is DeliveryRunState.WAITING_CI
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_current_view_merges_the_latest_merge_record(tmp_path: Path) -> None:
    store = DeliveryRunStore(tmp_path)
    merges = MergeStore()
    record = MergeRecord(
        repo="owner/name",
        pr_number=9,
        head_sha="b" * 40,
        state=MergeState.AWAITING_APPROVAL.value,
    )
    merges.save(record)
    run = replace(
        _run(DeliveryRunState.WAITING_CI),
        merge=record,
    )

    view = store.view(run, merges)

    assert view.merge == record
    assert view.state is DeliveryRunState.WAITING_CI


@pytest.mark.asyncio
async def test_audit_adapter_writes_transition_fields(monkeypatch, tmp_path: Path) -> None:
    entries = []

    async def capture(entry):
        entries.append(entry)

    monkeypatch.setattr("apps.code_review_pipeline.delivery.audit.record_audit", capture)
    audit = WalDeliveryAudit()
    previous = DeliveryRunState.IMPLEMENTING
    current = DeliveryRunState.CHECKING

    await audit.record(
        run=_run(current),
        previous=previous,
        current=current,
        detail={"action": "verify"},
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.trace_id == "trace-1"
    assert entry.agent_name == "delivery_lifecycle"
    assert entry.extra["run_id"] == "run-1"
    assert entry.extra["from_state"] == "implementing"
    assert entry.extra["to_state"] == "checking"
    assert entry.extra["action"] == "verify"
```

- [ ] **Step 2: Run the store tests to verify they fail**

Run: `uv run pytest tests/unit/test_delivery_store.py -q`

Expected: FAIL because `delivery.run_store` and `delivery.audit` do not exist.

- [ ] **Step 3: Implement atomic JSON persistence**

Create `apps/code_review_pipeline/delivery/run_store.py`.

```python
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.code_review_pipeline.change.bundle import ChangeBundle, FileChange
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    AgentAttempt,
    AgentStep,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    EvidenceRef,
    FailureCode,
    FailureInfo,
    PushOutcome,
    VerificationEvidence,
    VerifierVerdict,
)
from apps.code_review_pipeline.merge_store import MergeRecord


class DeliveryRunStore:
    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    def _path(self, run_id: str) -> Path:
        safe = "".join(
            char for char in run_id if char.isalnum() or char in {"-", "_", "."}
        )
        if not safe or safe != run_id:
            raise ValueError(f"invalid run_id: {run_id!r}")
        return self._directory / f"{safe}.json"

    async def save(self, run: DeliveryRun) -> None:
        await asyncio.to_thread(self._write, run)

    def _write(self, run: DeliveryRun) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path(run.run_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(run.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def load(self, run_id: str) -> DeliveryRun:
        path = self._path(run_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _run_from_dict(payload)

    def view(self, run: DeliveryRun, merge_store: Any) -> DeliveryRun:
        if run.push is None:
            return run
        record = merge_store.get(
            (
                run.request.repo,
                run.push.pull_request.number,
                run.push.pull_request.head_sha,
            )
        )
        return run if record is None else replace(run, merge=record)


def _request_from_dict(payload: dict[str, Any]) -> Any:
    from apps.code_review_pipeline.delivery.models import DeliveryRequest

    return DeliveryRequest(
        trace_id=str(payload["trace_id"]),
        repo=str(payload["repo"]),
        base_sha=str(payload["base_sha"]),
        base_branch=str(payload["base_branch"]),
        branch=str(payload["branch"]),
        goal=str(payload["goal"]),
        title=str(payload["title"]),
        body=str(payload.get("body", "")),
        origin=str(payload.get("origin", "manual")),
        source_ref=str(payload.get("source_ref", "")),
    )


def _attempt_from_dict(payload: dict[str, Any]) -> AgentAttempt:
    usage = dict(payload.get("usage") or {})
    return AgentAttempt(
        role=payload["role"],
        status=payload["status"],
        summary=str(payload.get("summary", "")),
        stop_reason=str(payload.get("stop_reason", "")),
        usage=DeliveryUsage(
            llm_calls=int(usage.get("llm_calls", 0) or 0),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
            llm_latency_ms=float(usage.get("llm_latency_ms", 0.0) or 0.0),
        ),
        steps=tuple(
            AgentStep(
                index=int(step["index"]),
                action=step["action"],
                tool=str(step.get("tool", "")),
                arguments=dict(step.get("arguments") or {}),
                observation=str(step.get("observation", "")),
                outcome=step.get("outcome", ""),
                elapsed_ms=float(step.get("elapsed_ms", 0.0) or 0.0),
            )
            for step in payload.get("steps", ())
        ),
    )


def _bundle_from_dict(payload: dict[str, Any]) -> ChangeBundle:
    return ChangeBundle(
        base_sha=str(payload["base_sha"]),
        branch=str(payload["branch"]),
        title=str(payload["title"]),
        body=str(payload.get("body", "")),
        files=tuple(
            FileChange(
                path=str(item["path"]),
                kind=str(item["kind"]),
                before_hash=str(item.get("before_hash", "")),
                after_hash=str(item.get("after_hash", "")),
            )
            for item in payload.get("files", ())
        ),
        patch=str(payload.get("patch", "")),
        patch_hash=str(payload["patch_hash"]),
        verify_command=tuple(
            str(item) for item in payload.get("verify_command", ())
        ),
        verify_exit_code=int(payload["verify_exit_code"]),
        verify_output=str(payload.get("verify_output", "")),
        model=str(payload["model"]),
        trace_id=str(payload.get("trace_id", "")),
    )


def _merge_from_dict(payload: dict[str, Any]) -> MergeRecord:
    return MergeRecord(
        repo=str(payload["repo"]),
        pr_number=int(payload["pr_number"]),
        head_sha=str(payload["head_sha"]),
        state=str(payload.get("state", "watching")),
        ci_state=str(payload.get("ci_state", "")),
        ci_failing=tuple(
            dict(item) for item in payload.get("ci_failing", ())
        ),
        card_message_id=str(payload.get("card_message_id", "")),
        approver=str(payload.get("approver", "")),
        merged_sha=str(payload.get("merged_sha", "")),
        failure_reason=str(payload.get("failure_reason", "")),
        transitions=tuple(
            dict(item) for item in payload.get("transitions", ())
        ),
    )


def _run_from_dict(payload: dict[str, Any]) -> DeliveryRun:
    schema_version = int(payload["schema_version"])
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported DeliveryRun schema: {schema_version} "
            f"(expected {SCHEMA_VERSION})"
        )

    attempts = tuple(
        _attempt_from_dict(dict(item))
        for item in payload.get("attempts", ())
    )
    verdicts = tuple(
        VerifierVerdict(
            decision=item["decision"],
            reason=str(item.get("reason", "")),
            instructions=str(item.get("instructions", "")),
            evidence=tuple(
                EvidenceRef(
                    kind=evidence["kind"],
                    ref=str(evidence["ref"]),
                    observation=str(evidence["observation"]),
                )
                for evidence in item.get("evidence", ())
            ),
            reviewed_bundle_hash=str(item["reviewed_bundle_hash"]),
            reviewed_patch_hash=str(item["reviewed_patch_hash"]),
            attempt=_attempt_from_dict(dict(item["attempt"])),
        )
        for item in payload.get("verifier_verdicts", ())
    )
    verification_payload = payload.get("verification")
    verification = (
        VerificationEvidence(
            command=tuple(
                str(item) for item in verification_payload.get("command", ())
            ),
            exit_code=int(verification_payload["exit_code"]),
            output_hash=str(verification_payload["output_hash"]),
            output_excerpt=str(verification_payload.get("output_excerpt", "")),
        )
        if verification_payload
        else None
    )
    bundle_payload = payload.get("bundle")
    bundle = _bundle_from_dict(dict(bundle_payload)) if bundle_payload else None
    push_payload = payload.get("push")
    push = (
        PushOutcome(
            branch=str(push_payload["branch"]),
            commit_sha=str(push_payload["commit_sha"]),
            pull_request=PullRequestRef(
                number=int(push_payload["pull_request"]["number"]),
                url=str(push_payload["pull_request"]["url"]),
                head_sha=str(push_payload["pull_request"]["head_sha"]),
            ),
        )
        if push_payload
        else None
    )
    merge_payload = payload.get("merge")
    merge = _merge_from_dict(dict(merge_payload)) if merge_payload else None
    failure_payload = payload.get("failure")
    failure = (
        FailureInfo(
            FailureCode(failure_payload["code"]),
            str(failure_payload["message"]),
        )
        if failure_payload
        else None
    )
    usage = dict(payload["usage"])
    return DeliveryRun(
        schema_version=schema_version,
        run_id=str(payload["run_id"]),
        trace_id=str(payload["trace_id"]),
        request=_request_from_dict(dict(payload["request"])),
        topology=payload["topology"],
        state=DeliveryRunState(payload["state"]),
        attempts=attempts,
        verification=verification,
        verifier_verdicts=verdicts,
        bundle=bundle,
        push=push,
        merge=merge,
        usage=DeliveryUsage(
            llm_calls=int(usage.get("llm_calls", 0) or 0),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
            llm_latency_ms=float(usage.get("llm_latency_ms", 0.0) or 0.0),
        ),
        failure=failure,
        started_at=datetime.fromisoformat(str(payload["started_at"])),
        updated_at=datetime.fromisoformat(str(payload["updated_at"])),
    )
```

The store is lossless for every field in `DeliveryRun.to_dict()`. The test suite
round-trips a run containing attempts, verification, a bundle, a push, a merge
record, verifier verdicts, and failure evidence, so later S1 work cannot silently
drop already-collected evidence.

- [ ] **Step 4: Implement the audit adapter**

Create `apps/code_review_pipeline/delivery/audit.py`.

```python
from __future__ import annotations

from app.audit.models import AuditEntry
from app.audit.recorder import record as record_audit
from apps.code_review_pipeline.delivery.models import (
    DeliveryRun,
    DeliveryRunState,
)


class WalDeliveryAudit:
    async def record(
        self,
        *,
        run: DeliveryRun,
        previous: DeliveryRunState | None,
        current: DeliveryRunState,
        detail: dict[str, object],
    ) -> None:
        await record_audit(
            AuditEntry(
                trace_id=run.trace_id,
                session_id=run.request.repo,
                agent_name="delivery_lifecycle",
                agent_output=current.value,
                intent="delivery_lifecycle",
                extra={
                    "run_id": run.run_id,
                    "from_state": previous.value if previous else "",
                    "to_state": current.value,
                    **detail,
                },
            )
        )
```

- [ ] **Step 5: Export the persistence and audit adapters**

Add to `apps/code_review_pipeline/delivery/__init__.py`:

```python
from apps.code_review_pipeline.delivery.audit import WalDeliveryAudit
from apps.code_review_pipeline.delivery.run_store import DeliveryRunStore
```

Add `"DeliveryRunStore"` and `"WalDeliveryAudit"` to `__all__`.

- [ ] **Step 6: Run the store tests and the audit regression**

Run:

```powershell
uv run pytest tests/unit/test_delivery_store.py tests/unit/test_audit_chain.py -q
uv run ruff check apps/code_review_pipeline/delivery tests/unit/test_delivery_store.py
```

Expected: all pass. `test_audit_chain.py` remains green, proving the adapter uses
the existing writer rather than creating a second WAL.

- [ ] **Step 7: Commit the persistence path**

```powershell
git add apps/code_review_pipeline/delivery/__init__.py apps/code_review_pipeline/delivery/run_store.py apps/code_review_pipeline/delivery/audit.py tests/unit/test_delivery_store.py
git commit -m "feat: persist delivery runs and audit transitions"
```

---

### Task 6: Extract a Structured, Injectably Scoped Tool Loop

**Files:**
- Modify: `app/agents/stubs.py:122-218`
- Test: `tests/unit/test_structured_tool_loop.py`
- Test: `tests/unit/test_agent_tools_loop.py`

**Interfaces:**
- Consumes: existing `ToolRegistry`, `call_workspace_tool`, `call_tool`, `ChatResult`, and workspace policy.
- Produces: `ToolLoopStep`, `ToolLoopUsage`, `ToolLoopResult`, `execute_tool_loop(...) -> ToolLoopResult`; existing `_execute_with_tools(...) -> str` becomes a compatibility wrapper.

- [ ] **Step 1: Write the failing structured-loop tests**

Create `tests/unit/test_structured_tool_loop.py`.

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.provider import ChatResult
from app.agents.stubs import execute_tool_loop
from app.agents.workspace_tools import build_toolset


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


class FakeLLM:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.last_metrics = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost_usd": 0.001,
            "llm_latency_ms": 2.0,
        }

    async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResult:
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
            tool_calls=response,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_tool_loop_returns_steps_and_usage(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            [_call("1", "write_file", {"path": "x.txt", "content": "ok"})],
            "finished",
        ]
    )
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=("python", "-c", "raise SystemExit(0)"),
        names=("read_file", "write_file", "search", "run_tests"),
    )

    result = await execute_tool_loop(
        llm=llm,
        messages=[
            {"role": "system", "content": "implement"},
            {"role": "user", "content": "write x"},
        ],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=4,
        max_tokens=100,
    )

    assert result.status == "completed"
    assert result.content == "finished"
    assert result.usage.llm_calls == 2
    assert result.usage.prompt_tokens == 20
    assert result.usage.completion_tokens == 10
    assert [step.action for step in result.steps] == ["call_tool", "finish"]
    assert result.steps[0].tool == "write_file"
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "ok\n"


@pytest.mark.asyncio
async def test_tool_loop_stops_on_token_budget(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            [_call("1", "read_file", {"path": "x.txt"})],
            [_call("2", "read_file", {"path": "x.txt"})],
        ]
    )
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=(),
        names=("read_file",),
    )

    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "read"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=5,
        max_tokens=20,
    )

    assert result.status == "budget_exhausted"
    assert result.usage.total_tokens == 30
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_structured_tool_loop.py -q`

Expected: FAIL because `app.agents.stubs.execute_tool_loop` does not exist.

- [ ] **Step 3: Add structured result types and the reusable loop**

Add to `app/agents/stubs.py`:

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ToolLoopStep:
    index: int
    action: Literal["call_tool", "finish"]
    tool: str
    arguments: dict[str, Any]
    observation: str
    outcome: Literal["ok", "rejected", "failed", ""]
    elapsed_ms: float


@dataclass(frozen=True)
class ToolLoopUsage:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add_metrics(self, metrics: dict[str, Any] | None) -> "ToolLoopUsage":
        values = metrics or {}
        return ToolLoopUsage(
            llm_calls=self.llm_calls + 1,
            prompt_tokens=self.prompt_tokens
            + int(values.get("prompt_tokens", 0) or 0),
            completion_tokens=self.completion_tokens
            + int(values.get("completion_tokens", 0) or 0),
            cost_usd=self.cost_usd + float(values.get("cost_usd", 0.0) or 0.0),
            llm_latency_ms=self.llm_latency_ms
            + float(values.get("llm_latency_ms", 0.0) or 0.0),
        )


@dataclass(frozen=True)
class ToolLoopResult:
    status: Literal["completed", "step_limit", "budget_exhausted", "error"]
    content: str
    steps: tuple[ToolLoopStep, ...]
    usage: ToolLoopUsage
    stop_reason: str
```

Add `execute_tool_loop` immediately before `_execute_with_tools`.

```python
async def execute_tool_loop(
    *,
    llm: Any,
    messages: list[dict[str, Any]],
    tools: Any,
    workspace: Path | None,
    agent_name: str,
    max_steps: int,
    max_tokens: int = 0,
    slot: dict[str, object] | None = None,
) -> ToolLoopResult:
    import time

    slot = slot if slot is not None else {}
    logger.info("%s structured tool loop", agent_name)
    chat_with_tools = getattr(llm, "chat_with_tools", None)
    if chat_with_tools is None:
        content = await llm.chat(messages)
        return ToolLoopResult(
            status="completed",
            content=content,
            steps=(
                ToolLoopStep(
                    index=1,
                    action="finish",
                    tool="",
                    arguments={},
                    observation=content[:200],
                    outcome="",
                    elapsed_ms=0.0,
                ),
            ),
            usage=ToolLoopUsage(),
            stop_reason="model finished",
        )

    schemas = tools.list_schemas()
    steps: list[ToolLoopStep] = []
    usage = ToolLoopUsage()
    current_messages = list(messages)
    try:
        result = await chat_with_tools(current_messages, schemas)
        usage = usage.add_metrics(getattr(llm, "last_metrics", None))
        for round_index in range(max_steps):
            if not result.tool_calls:
                steps.append(
                    ToolLoopStep(
                        index=len(steps) + 1,
                        action="finish",
                        tool="",
                        arguments={},
                        observation=str(result.content or "")[:200],
                        outcome="",
                        elapsed_ms=0.0,
                    )
                )
                return ToolLoopResult(
                    status="completed",
                    content=str(result.content or ""),
                    steps=tuple(steps),
                    usage=usage,
                    stop_reason="model finished",
                )

            if max_tokens > 0 and usage.total_tokens >= max_tokens:
                return ToolLoopResult(
                    status="budget_exhausted",
                    content=str(result.content or ""),
                    steps=tuple(steps),
                    usage=usage,
                    stop_reason=(
                        f"token budget exhausted: {usage.total_tokens} >= {max_tokens}"
                    ),
                )

            for call in result.tool_calls:
                started = time.monotonic()
                name = str((call.get("function") or {}).get("name", ""))
                arguments: dict[str, Any] = {}
                slot["tool_calls_total"] = int(slot.get("tool_calls_total", 0) or 0) + 1
                tool = tools.get(name)
                outcome_kind: Literal["ok", "rejected", "failed", ""] = "failed"
                if tool is None:
                    observation = '{"error": "unknown tool"}'
                    slot["tool_errors_total"] = int(
                        slot.get("tool_errors_total", 0) or 0
                    ) + 1
                else:
                    try:
                        arguments = json.loads(
                            (call.get("function") or {}).get("arguments") or "{}"
                        )
                    except json.JSONDecodeError as exc:
                        observation = f'{{"error": "参数不是合法 JSON: {exc}"}}'
                        outcome_kind = "rejected"
                        slot["tool_arg_rejections_total"] = int(
                            slot.get("tool_arg_rejections_total", 0) or 0
                        ) + 1
                    else:
                        outcome = await (
                            call_workspace_tool(
                                tool,
                                arguments,
                                workspace=workspace,
                            )
                            if workspace is not None
                            else call_tool(
                                tool,
                                arguments,
                                session_id=str(slot.get("session_id", "")),
                            )
                        )
                        outcome_kind = outcome.kind
                        if outcome.kind == "ok":
                            observation = outcome.detail
                        elif outcome.kind == "rejected":
                            observation = f'{{"error": "参数被拒：{outcome.detail}"}}'
                            slot["tool_arg_rejections_total"] = int(
                                slot.get("tool_arg_rejections_total", 0) or 0
                            ) + 1
                        else:
                            observation = f'{{"error": "{outcome.detail}"}}'
                            slot["tool_errors_total"] = int(
                                slot.get("tool_errors_total", 0) or 0
                            ) + 1
                steps.append(
                    ToolLoopStep(
                        index=len(steps) + 1,
                        action="call_tool",
                        tool=name,
                        arguments=(
                            arguments if isinstance(arguments, dict) else {}
                        ),
                        observation=observation,
                        outcome=outcome_kind,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                )
                result.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": observation,
                    }
                )
            current_messages = result.messages
            result = await chat_with_tools(current_messages, schemas)
            usage = usage.add_metrics(getattr(llm, "last_metrics", None))
        pending = ", ".join(
            str((call.get("function") or {}).get("name", ""))
            for call in result.tool_calls
        )
        return ToolLoopResult(
            status="step_limit",
            content=str(result.content or ""),
            steps=tuple(steps),
            usage=usage,
            stop_reason=f"tool step limit reached; pending: {pending}",
        )
    except Exception as exc:
        return ToolLoopResult(
            status="error",
            content="",
            steps=tuple(steps),
            usage=usage,
            stop_reason=f"{type(exc).__name__}: {exc}",
        )
```

Refactor `_execute_with_tools` to call this primitive rather than retaining a second loop.

```python
async def _execute_with_tools(
    llm: LLMClient,
    envelope: AgentEnvelope,
    role_tag: str,
    extra_instructions: str,
    agent_name: str,
) -> str:
    override = (envelope.agent_local_slot or {}).get("system_prompt", "")
    system = (
        str(override)
        if override
        else _build_envelope_context(envelope, role_tag) + extra_instructions
    )
    messages = [{"role": "system", "content": system}]
    messages.extend(envelope.history or ())
    messages.append({"role": "user", "content": envelope.user_raw_input})
    tools, workspace = _tool_runtime(envelope)
    slot = dict(envelope.agent_local_slot or {})
    slot["session_id"] = envelope.session_id
    result = await execute_tool_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        workspace=workspace,
        agent_name=agent_name,
        max_steps=settings.agent_max_steps,
        slot=slot,
    )
    envelope.agent_local_slot.update(slot)
    if result.status == "error":
        raise RuntimeError(result.stop_reason)
    if result.status == "step_limit":
        return result.content + f"\n[工具调用已达上限] {result.stop_reason}"
    return result.content
```

- [ ] **Step 4: Run the structured and legacy loop tests**

Run:

```powershell
uv run pytest tests/unit/test_structured_tool_loop.py tests/unit/test_agent_tools_loop.py -q
uv run ruff check app/agents/stubs.py tests/unit/test_structured_tool_loop.py tests/unit/test_agent_tools_loop.py
```

Expected: the new structured tests pass and all existing tool-loop tests keep their prior behavior.

- [ ] **Step 5: Commit the loop extraction**

```powershell
git add app/agents/stubs.py tests/unit/test_structured_tool_loop.py tests/unit/test_agent_tools_loop.py
git commit -m "refactor: expose structured agent tool loop"
```

---

### Task 7: Implement the Production Implementer Adapter

**Files:**
- Create: `apps/code_review_pipeline/delivery/adapters/__init__.py`
- Create: `apps/code_review_pipeline/delivery/adapters/production_agent.py`
- Test: `tests/unit/test_delivery_production_agent.py`

**Interfaces:**
- Consumes: `execute_tool_loop(...) -> ToolLoopResult` from Task 6; `build_toolset`; `ImplementerPort`.
- Produces: `ProductionImplementer(llm, verify_command, system_prompt="...")` implementing `ImplementerPort.run(...)`.

- [ ] **Step 1: Write the failing production adapter tests**

Create `tests/unit/test_delivery_production_agent.py`.

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.provider import ChatResult
from apps.code_review_pipeline.delivery.adapters.production_agent import (
    ProductionImplementer,
)


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


class FakeImplementerLLM:
    def __init__(self) -> None:
        self.messages: list[list[dict]] = []
        self.last_metrics = {
            "prompt_tokens": 100,
            "completion_tokens": 25,
            "cost_usd": 0.002,
            "llm_latency_ms": 12.0,
        }
        self._responses = [
            [
                _call(
                    "1",
                    "write_file",
                    {"path": "app.py", "content": "def add(a, b):\n    return a + b\n"},
                )
            ],
            "implemented the fix",
        ]

    async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResult:
        self.messages.append([dict(item) for item in messages])
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
            tool_calls=response,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_implementer_returns_structured_attempt_and_writes_file(
    tmp_path: Path,
) -> None:
    llm = FakeImplementerLLM()
    adapter = ProductionImplementer(
        llm=llm,
        verify_command=("python", "-m", "pytest", "-q"),
    )

    attempt = await adapter.run(
        workspace=tmp_path,
        goal="fix add",
        feedback="the previous version returned a - b",
        max_steps=5,
        max_tokens=1000,
    )

    assert attempt.role == "implementer"
    assert attempt.status == "completed"
    assert attempt.summary == "implemented the fix"
    assert attempt.usage.llm_calls == 2
    assert attempt.usage.prompt_tokens == 200
    assert attempt.usage.completion_tokens == 50
    assert attempt.steps[0].tool == "write_file"
    assert "return a + b" in (tmp_path / "app.py").read_text(encoding="utf-8")
    assert "the previous version returned a - b" in "\n".join(
        str(item["content"])
        for item in llm.messages[0]
        if item.get("role") == "user"
    )


@pytest.mark.asyncio
async def test_implementer_has_no_write_tool_when_not_requested_by_registry(
    tmp_path: Path,
) -> None:
    class UnknownToolLLM(FakeImplementerLLM):
        def __init__(self) -> None:
            super().__init__()
            self._responses = [
                [_call("1", "git_push", {"branch": "main"})],
                "stopped without using GitHub",
            ]

    adapter = ProductionImplementer(
        llm=UnknownToolLLM(),
        verify_command=("python", "-c", "raise SystemExit(0)"),
    )

    attempt = await adapter.run(
        workspace=tmp_path,
        goal="fix",
        feedback="",
        max_steps=3,
        max_tokens=1000,
    )

    assert attempt.steps[0].outcome == "failed"
    assert "unknown tool" in attempt.steps[0].observation
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_delivery_production_agent.py -q`

Expected: FAIL because `delivery.adapters.production_agent` does not exist.

- [ ] **Step 3: Implement the adapter**

Create `apps/code_review_pipeline/delivery/adapters/production_agent.py`.

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.stubs import execute_tool_loop
from app.agents.workspace_tools import build_toolset
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    AgentStep,
    DeliveryUsage,
)

_IMPLEMENTER_SYSTEM = """\
You are the Implementer in a governed repository delivery pipeline.
Modify only the provided workspace. Use the available file and test tools.
You cannot access GitHub, create approvals, push, or merge.
Read before editing. When the goal is complete, stop calling tools and summarize.
"""


class ProductionImplementer:
    def __init__(
        self,
        *,
        llm: Any,
        verify_command: tuple[str, ...],
        system_prompt: str = _IMPLEMENTER_SYSTEM,
    ) -> None:
        self._llm = llm
        self._verify_command = tuple(verify_command)
        self._system_prompt = system_prompt

    async def run(
        self,
        *,
        workspace: Path,
        goal: str,
        feedback: str,
        max_steps: int,
        max_tokens: int,
    ) -> AgentAttempt:
        root = Path(workspace).resolve()
        names = ["read_file", "write_file", "search"]
        if self._verify_command:
            names.append("run_tests")
        tools = build_toolset(
            workspace=root,
            verify_command=self._verify_command,
            names=tuple(names),
        )
        user_parts = [f"Goal:\n{goal}"]
        if feedback.strip():
            user_parts.append(f"Required revision:\n{feedback.strip()}")
        result = await execute_tool_loop(
            llm=self._llm,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            tools=tools,
            workspace=root,
            agent_name="delivery_implementer",
            max_steps=max_steps,
            max_tokens=max_tokens,
        )
        return AgentAttempt(
            role="implementer",
            status=result.status,
            summary=result.content.strip()[:1000],
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
```

Create `apps/code_review_pipeline/delivery/adapters/__init__.py`.

```python
from apps.code_review_pipeline.delivery.adapters.production_agent import (
    ProductionImplementer,
)

__all__ = ["ProductionImplementer"]
```

- [ ] **Step 4: Run the adapter tests**

Run:

```powershell
uv run pytest tests/unit/test_delivery_production_agent.py tests/unit/test_structured_tool_loop.py -q
uv run ruff check apps/code_review_pipeline/delivery/adapters app/agents/stubs.py tests/unit/test_delivery_production_agent.py
```

Expected: all pass and Ruff passes.

- [ ] **Step 5: Commit the production adapter**

```powershell
git add apps/code_review_pipeline/delivery/adapters/__init__.py apps/code_review_pipeline/delivery/adapters/production_agent.py tests/unit/test_delivery_production_agent.py
git commit -m "feat: add production delivery implementer"
```

---

### Task 8: Connect the Ports to the Existing Change Pipeline, GitHub, and Merge Store

**Files:**
- Create: `apps/code_review_pipeline/delivery/adapters/github.py`
- Modify: `apps/code_review_pipeline/delivery/adapters/__init__.py`
- Modify: `apps/code_review_pipeline/change/pipeline.py`
- Test: `tests/unit/test_delivery_github_adapter.py`

**Interfaces:**
- Consumes: `ChangePipeline.prepare_bundle`, `ChangePipeline.publish_bundle`, `ChangeResult.pull_request`, `PullRequestRef`, `MergeStore.save`, `MergeStore.supersede_older`, and `initial_transition`.
- Produces: `GitHubChangeAdapter(pipeline, merge_store, clock=...)` implementing both `ChangePreparerPort` and `ChangePublisher`.

- [ ] **Step 1: Write the failing adapter tests**

Create `tests/unit/test_delivery_github_adapter.py`.

```python
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.pipeline import (
    ChangeApproval,
    ChangeResult,
    ChangeStatus,
)
from apps.code_review_pipeline.delivery.adapters.github import GitHubChangeAdapter
from apps.code_review_pipeline.delivery.models import DeliveryPolicy, DeliveryRequest
from apps.code_review_pipeline.delivery.ports import PreparationStatus
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeStore
from apps.code_review_pipeline.change.refs import PullRequestRef


def _request() -> DeliveryRequest:
    return DeliveryRequest(
        trace_id="trace-1",
        repo="owner/name",
        base_sha="a" * 40,
        base_branch="main",
        branch="moa/fix",
        goal="fix",
        title="fix",
        body="",
    )


def _bundle() -> ChangeBundle:
    return ChangeBundle(
        base_sha="a" * 40,
        branch="moa/fix-trace-1",
        title="fix",
        body="",
        files=(),
        patch="",
        patch_hash="p" * 64,
        verify_command=("pytest",),
        verify_exit_code=0,
        verify_output="1 passed",
        model="test-model",
        trace_id="trace-1",
    )


class FakePipeline:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self.prepare_calls = 0
        self.publish_calls = 0

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def repo(self) -> str:
        return "owner/name"

    @property
    def verify_command(self) -> tuple[str, ...]:
        return ("pytest",)

    async def prepare_bundle(self, request):
        self.prepare_calls += 1
        return ChangeResult(
            status=ChangeStatus.VERIFIED,
            bundle=_bundle(),
            message="ready",
        )

    async def publish_bundle(self, bundle, approval):
        self.publish_calls += 1
        return ChangeResult(
            status=ChangeStatus.PUSHED,
            bundle=bundle,
            message="pushed",
            pr_url="https://example.com/pr/5",
            commit_sha="b" * 40,
            pull_request=PullRequestRef(
                number=5,
                url="https://example.com/pr/5",
                head_sha="b" * 40,
            ),
        )


@pytest.mark.asyncio
async def test_preparer_maps_verified_change_into_port_result(tmp_path: Path) -> None:
    pipeline = FakePipeline(tmp_path)
    adapter = GitHubChangeAdapter(pipeline=pipeline, merge_store=MergeStore())

    result = await adapter.prepare(
        workspace=tmp_path,
        request=_request(),
        policy=DeliveryPolicy(verify_command=("pytest",)),
    )

    assert result.status is PreparationStatus.READY
    assert result.bundle is not None
    assert result.bundle.bundle_hash == _bundle().bundle_hash
    assert result.verification is not None
    assert result.verification.output_excerpt == "1 passed"
    assert pipeline.prepare_calls == 1


@pytest.mark.asyncio
async def test_publisher_registers_merge_record_without_webhook(tmp_path: Path) -> None:
    pipeline = FakePipeline(tmp_path)
    merges = MergeStore()
    adapter = GitHubChangeAdapter(
        pipeline=pipeline,
        merge_store=merges,
        clock=lambda: datetime(2026, 10, 27, tzinfo=UTC),
    )
    bundle = _bundle()
    approval = ChangeApproval(
        approver="alice",
        trace_id="trace-1",
        bundle_hash=bundle.bundle_hash,
    )

    push = await adapter.publish(
        workspace=tmp_path,
        bundle=bundle,
        approval=approval,
    )

    record = merges.get(("owner/name", 5, "b" * 40))
    assert push.pull_request.number == 5
    assert record is not None
    assert record.state == MergeState.WATCHING.value
    assert record.transitions[0]["to"] == MergeState.WATCHING.value
    assert pipeline.publish_calls == 1


@pytest.mark.asyncio
async def test_adapter_refuses_a_workspace_different_from_pipeline(tmp_path: Path) -> None:
    pipeline = FakePipeline(tmp_path / "bound")
    adapter = GitHubChangeAdapter(pipeline=pipeline, merge_store=MergeStore())

    with pytest.raises(ValueError, match="workspace"):
        await adapter.prepare(
            workspace=tmp_path / "other",
            request=_request(),
            policy=DeliveryPolicy(verify_command=("pytest",)),
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_delivery_github_adapter.py -q`

Expected: FAIL because `delivery.adapters.github` does not exist and `ChangePipeline` has no public `workspace` property.

- [ ] **Step 3: Expose the bound workspace and repository on `ChangePipeline`**

Add these properties to `ChangePipeline`.

```python
    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def repo(self) -> str:
        return self._repo

    @property
    def verify_command(self) -> tuple[str, ...]:
        return self._verify_command
```

- [ ] **Step 4: Implement the GitHub adapter**

Create `apps/code_review_pipeline/delivery/adapters/github.py`.

```python
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.pipeline import (
    ChangeApproval,
    ChangeBundleChanged,
    ChangePipeline,
    ChangeRequest,
    ChangeStatus,
)
from apps.code_review_pipeline.delivery.models import (
    DeliveryPolicy,
    DeliveryRequest,
    PushOutcome,
    VerificationEvidence,
)
from apps.code_review_pipeline.delivery.ports import (
    BundleChanged,
    ChangePreparation,
    PreparationStatus,
)
from apps.code_review_pipeline.merge_state import initial_transition
from apps.code_review_pipeline.merge_store import MergeRecord


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GitHubChangeAdapter:
    def __init__(
        self,
        *,
        pipeline: ChangePipeline,
        merge_store: Any,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._pipeline = pipeline
        self._merge_store = merge_store
        self._clock = clock

    def _require_workspace(self, workspace: Path) -> Path:
        root = Path(workspace).resolve()
        if root != self._pipeline.workspace.resolve():
            raise ValueError(
                f"workspace differs from pipeline workspace: {root} "
                f"!= {self._pipeline.workspace.resolve()}"
            )
        return root

    @property
    def repo(self) -> str:
        return self._pipeline.repo

    @property
    def verify_command(self) -> tuple[str, ...]:
        return self._pipeline.verify_command

    async def prepare(
        self,
        *,
        workspace: Path,
        request: DeliveryRequest,
        policy: DeliveryPolicy,
    ) -> ChangePreparation:
        self._require_workspace(workspace)
        if tuple(policy.verify_command) != self._pipeline.verify_command:
            raise ValueError(
                "delivery policy verify_command differs from change pipeline"
            )
        result = await self._pipeline.prepare_bundle(
            ChangeRequest(
                repo=request.repo,
                base_sha=request.base_sha,
                title=request.title,
                body=request.body,
                goal=request.goal,
                branch=request.branch,
                trace_id=request.trace_id,
            )
        )
        if result.status is ChangeStatus.VERIFIED:
            assert result.bundle is not None
            output = result.bundle.verify_output
            verification = VerificationEvidence(
                command=result.bundle.verify_command,
                exit_code=result.bundle.verify_exit_code,
                output_hash=hashlib.sha256(output.encode("utf-8")).hexdigest(),
                output_excerpt=output[-4000:],
            )
            return ChangePreparation(
                status=PreparationStatus.READY,
                bundle=result.bundle,
                verification=verification,
                message=result.message,
            )
        if result.status is ChangeStatus.VERIFY_FAILED:
            assert result.bundle is not None
            output = result.bundle.verify_output
            return ChangePreparation(
                status=PreparationStatus.VERIFY_FAILED,
                bundle=result.bundle,
                verification=VerificationEvidence(
                    command=result.bundle.verify_command,
                    exit_code=result.bundle.verify_exit_code,
                    output_hash=hashlib.sha256(output.encode("utf-8")).hexdigest(),
                    output_excerpt=output[-4000:],
                ),
                message=result.message,
            )
        if result.status is ChangeStatus.NO_CHANGES:
            return ChangePreparation(
                status=PreparationStatus.NO_CHANGES,
                bundle=result.bundle,
                verification=None,
                message=result.message,
            )
        return ChangePreparation(
            status=PreparationStatus.INVALID,
            bundle=result.bundle,
            verification=None,
            message=result.message or result.status.value,
        )

    async def publish(
        self,
        *,
        workspace: Path,
        bundle: ChangeBundle,
        approval: ChangeApproval,
    ) -> PushOutcome:
        self._require_workspace(workspace)
        try:
            result = await self._pipeline.publish_bundle(bundle, approval)
        except ChangeBundleChanged as exc:
            raise BundleChanged(str(exc)) from exc
        if result.status is not ChangeStatus.PUSHED:
            raise RuntimeError(
                f"change pipeline refused publish: {result.status.value}: "
                f"{result.message}"
            )
        if result.pull_request is None or not result.commit_sha:
            raise RuntimeError("pushed change did not return PR identity and commit SHA")
        push = PushOutcome(
            branch=bundle.branch,
            commit_sha=result.commit_sha,
            pull_request=result.pull_request,
        )
        now = self._clock().isoformat()
        record = MergeRecord(
            repo=self._pipeline.repo,
            pr_number=push.pull_request.number,
            head_sha=push.pull_request.head_sha,
            transitions=tuple(initial_transition(now)),
        )
        self._merge_store.save(record)
        self._merge_store.supersede_older(
            record.repo,
            record.pr_number,
            record.head_sha,
            at=now,
        )
        return push
```

Update `apps/code_review_pipeline/delivery/adapters/__init__.py`.

```python
from apps.code_review_pipeline.delivery.adapters.github import GitHubChangeAdapter
from apps.code_review_pipeline.delivery.adapters.production_agent import (
    ProductionImplementer,
)

__all__ = ["GitHubChangeAdapter", "ProductionImplementer"]
```

- [ ] **Step 5: Run adapter and merge-channel regressions**

Run:

```powershell
uv run pytest tests/unit/test_delivery_github_adapter.py tests/unit/test_change_pipeline.py tests/unit/test_merge_poller.py tests/unit/test_merge_executor.py -q
uv run ruff check apps/code_review_pipeline/delivery/adapters apps/code_review_pipeline/change/pipeline.py tests/unit/test_delivery_github_adapter.py
```

Expected: all pass. The adapter test proves the merge record exists before any webhook.

- [ ] **Step 6: Commit the GitHub adapter**

```powershell
git add apps/code_review_pipeline/delivery/adapters/github.py apps/code_review_pipeline/delivery/adapters/__init__.py apps/code_review_pipeline/change/pipeline.py tests/unit/test_delivery_github_adapter.py
git commit -m "feat: connect delivery flow to github merge channel"
```

---

### Task 9: Add Workspace Materialization, the CLI Entry, and Boundary Tests

**Files:**
- Create: `apps/code_review_pipeline/delivery/workspace.py`
- Modify: `app/cli.py:16-360`
- Modify: `apps/code_review_pipeline/delivery/__init__.py`
- Test: `tests/unit/test_delivery_workspace.py`
- Test: `tests/unit/test_delivery_cli.py`
- Test: `tests/unit/test_delivery_import_boundaries.py`

**Interfaces:**
- Consumes: `GitHubClient.download_archive`, settings, `LLMConfig`/`LLMClient`, `ProductionImplementer`, `ChangePipeline`, `GitHubChangeAdapter`, `DeliveryRunStore`, `WalDeliveryAudit`, `DeliveryFlow`.
- Produces: `GitHubWorkspaceProvisioner.materialize(...)`, CLI `delivery run`, and `TerminalPushApprovalPort`.

- [ ] **Step 1: Write the failing workspace, CLI, and boundary tests**

Create `tests/unit/test_delivery_workspace.py`.

```python
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apps.code_review_pipeline.delivery.workspace import GitHubWorkspaceProvisioner


class FakeArchiveGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path]] = []

    async def download_archive(self, repo, ref: str, dest: Path) -> None:
        self.calls.append((repo.owner, ref, dest))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "app.py").write_text("VALUE = 1\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_workspace_is_a_local_git_baseline(tmp_path: Path) -> None:
    github = FakeArchiveGitHub()
    provisioner = GitHubWorkspaceProvisioner(github=github)
    destination = tmp_path / "workspace"

    await provisioner.materialize(
        repo="owner/name",
        base_sha="a" * 40,
        destination=destination,
    )

    assert github.calls[0][:2] == ("owner", "a" * 40)
    assert (destination / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=destination,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""
```

Create `tests/unit/test_delivery_cli.py`.

```python
from __future__ import annotations

from app.cli import build_parser


def test_delivery_run_parser_requires_repo_and_base_sha() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "delivery",
            "run",
            "--repo",
            "owner/name",
            "--base-sha",
            "a" * 40,
            "--branch",
            "moa/fix-1",
            "--title",
            "fix: add",
            "--goal",
            "make add return a + b",
        ]
    )

    assert args.group == "delivery"
    assert args.command == "run"
    assert args.repo == "owner/name"
    assert args.base_sha == "a" * 40
    assert args.origin == "manual"
```

Create `tests/unit/test_delivery_import_boundaries.py`.

```python
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_product_core_does_not_import_bench() -> None:
    offenders: list[str] = []
    for path in (ROOT / "apps" / "code_review_pipeline" / "delivery").rglob("*.py"):
        if path.name == "bench_agent.py" or "bench" in path.parts:
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith(("import bench", "from bench")):
                offenders.append(f"{path.relative_to(ROOT)}:{line_no}")
    assert offenders == []


def test_flow_does_not_import_concrete_github_or_llm_clients() -> None:
    text = (
        ROOT
        / "apps"
        / "code_review_pipeline"
        / "delivery"
        / "flow.py"
    ).read_text(encoding="utf-8")
    assert "GitHubClient" not in text
    assert "LLMClient" not in text
    assert "ChangePipeline" not in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
uv run pytest tests/unit/test_delivery_workspace.py tests/unit/test_delivery_cli.py tests/unit/test_delivery_import_boundaries.py -q
```

Expected: FAIL because the workspace module, CLI subcommand, and boundary file are missing.

- [ ] **Step 3: Implement workspace materialization**

Create `apps/code_review_pipeline/delivery/workspace.py`.

```python
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from apps.code_review_pipeline.routing.github_client import GitHubRepo


def _git(args: list[str], *, cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 - arguments are constructed locally
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.strip()


def _init_baseline(destination: Path) -> None:
    _git(["init", "-q"], cwd=destination)
    _git(["config", "user.email", "delivery@moa.local"], cwd=destination)
    _git(["config", "user.name", "MoA Delivery"], cwd=destination)
    _git(["add", "."], cwd=destination)
    _git(["commit", "-q", "-m", "materialized base snapshot"], cwd=destination)


class GitHubWorkspaceProvisioner:
    def __init__(self, *, github: Any) -> None:
        self._github = github

    async def materialize(
        self,
        *,
        repo: str,
        base_sha: str,
        destination: Path,
    ) -> Path:
        owner, separator, name = repo.partition("/")
        if not separator or not owner or not name:
            raise ValueError(f"repo must be owner/name: {repo!r}")
        root = Path(destination).resolve()
        if root.exists() and any(root.iterdir()):
            raise ValueError(f"destination must be empty: {root}")
        root.mkdir(parents=True, exist_ok=True)
        await self._github.download_archive(
            GitHubRepo(owner=owner, name=name),
            base_sha,
            root,
        )
        await asyncio.to_thread(_init_baseline, root)
        return root
```

The local Git baseline is intentionally separate from the remote `base_sha`.
`ChangeBundle.base_sha` remains the remote SHA; the local baseline exists only so
`build_change_bundle` can create a deterministic worktree diff without putting a
token in a Git URL or depending on the host's Git credentials.

- [ ] **Step 4: Add the terminal approval adapter and CLI command**

Add imports at the top of `app/cli.py`.

```python
import tempfile
from dataclasses import replace
from pathlib import Path
```

Add `TerminalPushApprovalPort` before `build_parser`.

```python
class TerminalPushApprovalPort:
    async def request(self, *, run_id: str, bundle) -> Any:
        from apps.code_review_pipeline.change.pipeline import ChangeApproval

        print(f"run_id   : {run_id}")
        print(f"branch   : {bundle.branch}")
        print(f"bundle   : {bundle.bundle_hash}")
        print(f"patch    : {bundle.patch_hash}")
        print(f"verify   : exit={bundle.verify_exit_code}")
        if not sys.stdin.isatty():
            print("non-interactive stdin; push approval denied")
            return None
        operator = (await asyncio.to_thread(input, "approver: ")).strip()
        answer = (
            await asyncio.to_thread(
                input,
                "type APPROVE to push this exact bundle: ",
            )
        ).strip()
        if not operator or answer != "APPROVE":
            return None
        return ChangeApproval(
            approver=operator,
            trace_id=bundle.trace_id,
            bundle_hash=bundle.bundle_hash,
        )
```

Add `cmd_delivery_run` before `build_parser`.

```python
def cmd_delivery_run(args: argparse.Namespace) -> int:
    from app.agents.provider import LLMClient, LLMConfig
    from app.config import settings
    from apps.code_review_pipeline.change.pipeline import (
        ChangePipeline,
        verify_command_from_env,
    )
    from apps.code_review_pipeline.delivery.adapters.github import (
        GitHubChangeAdapter,
    )
    from apps.code_review_pipeline.delivery.adapters.production_agent import (
        ProductionImplementer,
    )
    from apps.code_review_pipeline.delivery.audit import WalDeliveryAudit
    from apps.code_review_pipeline.delivery.flow import DeliveryFlow
    from apps.code_review_pipeline.delivery.models import (
        DeliveryPolicy,
        DeliveryRequest,
    )
    from apps.code_review_pipeline.delivery.run_store import DeliveryRunStore
    from apps.code_review_pipeline.delivery.workspace import (
        GitHubWorkspaceProvisioner,
    )
    from apps.code_review_pipeline.merge_store import (
        PostgresMergeStore,
        build_merge_store,
    )
    from apps.code_review_pipeline.routing.github_provider import build_github_client

    if not settings.change_agent_push_enabled:
        print(
            "CHANGE_AGENT_PUSH_ENABLED=0；delivery run 是真实交付入口。"
            "dry-run 请使用 scripts/run_change_demo.py。",
            file=sys.stderr,
        )
        return 2
    if not settings.change_agent_model.strip():
        print("CHANGE_AGENT_MODEL 未配置", file=sys.stderr)
        return 2
    verify_command = verify_command_from_env(settings.change_agent_verify_command)
    if not verify_command:
        print("CHANGE_AGENT_VERIFY_COMMAND 未配置", file=sys.stderr)
        return 2
    if not settings.github_token:
        print("GITHUB_TOKEN 未配置", file=sys.stderr)
        return 2

    merge_store = build_merge_store()
    if not isinstance(merge_store, PostgresMergeStore):
        print(
            "delivery run 需要共享 MergeStore；请配置 "
            "VECTOR_DB_DSN 或 CODE_REVIEW_DATABASE_URL",
            file=sys.stderr,
        )
        return 2

    config = LLMConfig.from_env("LLM")
    config.model = settings.change_agent_model
    llm = LLMClient(config)
    github = build_github_client()
    policy = DeliveryPolicy(
        verify_command=verify_command,
        topology=settings.delivery_topology,
        max_steps=settings.delivery_max_steps,
        max_tokens=settings.delivery_max_tokens,
    )
    request = DeliveryRequest(
        trace_id=args.trace_id,
        repo=args.repo,
        base_sha=args.base_sha,
        base_branch=args.base_branch,
        branch=args.branch,
        goal=args.goal,
        title=args.title,
        body=args.body,
        origin=args.origin,
        source_ref=args.source_ref,
    )

    async def execute():
        try:
            with tempfile.TemporaryDirectory(prefix="moa-delivery-") as raw:
                workspace = await GitHubWorkspaceProvisioner(
                    github=github
                ).materialize(
                    repo=args.repo,
                    base_sha=args.base_sha,
                    destination=Path(raw) / "workspace",
                )
                pipeline = ChangePipeline(
                    github=github,
                    repo=args.repo,
                    workspace=workspace,
                    verify_command=verify_command,
                    model_name=settings.change_agent_model,
                    base_branch=args.base_branch,
                    push_enabled=True,
                )
                adapter = GitHubChangeAdapter(
                    pipeline=pipeline,
                    merge_store=merge_store,
                )
                flow = DeliveryFlow(
                    workspace=workspace,
                    policy=policy,
                    implementer=ProductionImplementer(
                        llm=llm,
                        verify_command=verify_command,
                    ),
                    preparer=adapter,
                    approval=TerminalPushApprovalPort(),
                    publisher=adapter,
                    recorder=DeliveryRunStore(settings.delivery_run_record_dir),
                    audit=WalDeliveryAudit(),
                )
                return await flow.run(request)
        finally:
            await merge_store.close()
            await llm.aclose()
            await github.aclose()

    run = asyncio.run(execute())
    print(f"run_id    : {run.run_id}")
    print(f"state     : {run.state.value}")
    print(f"failure   : {run.failure.to_dict() if run.failure else '-'}")
    if run.push is not None:
        print(f"pr        : {run.push.pull_request.url}")
        print(f"head_sha  : {run.push.pull_request.head_sha}")
    print(f"record    : {settings.delivery_run_record_dir}/{run.run_id}.json")
    return 0 if run.state.value in {"waiting_ci", "ready_for_push_approval"} else 1
```

Extend `build_parser`.

```python
    delivery = sub.add_parser("delivery", help="受治理的仓库交付")
    delivery_sub = delivery.add_subparsers(dest="command", required=True)
    delivery_run = delivery_sub.add_parser("run", help="实现、验证、审批、推送并登记 PR")
    delivery_run.add_argument("--repo", required=True)
    delivery_run.add_argument("--base-sha", required=True)
    delivery_run.add_argument("--base-branch", default=None)
    delivery_run.add_argument("--branch", required=True)
    delivery_run.add_argument("--title", required=True)
    delivery_run.add_argument("--body", default="")
    delivery_run.add_argument("--goal", required=True)
    delivery_run.add_argument(
        "--origin",
        choices=["manual", "issue", "ci_failure"],
        default="manual",
    )
    delivery_run.add_argument("--source-ref", default="")
    delivery_run.add_argument("--trace-id", default=None)
    delivery_run.set_defaults(func=cmd_delivery_run)
```

Before calling `parse_args` in `main`, set defaults that depend on settings.

```python
def _apply_delivery_defaults(args: argparse.Namespace) -> None:
    if getattr(args, "group", "") != "delivery":
        return
    from app.config import settings

    if args.base_branch is None:
        args.base_branch = settings.change_agent_base_branch
    if args.trace_id is None:
        args.trace_id = f"delivery-{time.time_ns()}"
    if not args.body:
        args.body = args.goal


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _apply_delivery_defaults(args)
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return int(args.func(args))
```

Replace the existing `main` function with this version.

- [ ] **Step 5: Run the focused S0 suite**

Run:

```powershell
uv run pytest tests/unit/test_delivery_workspace.py tests/unit/test_delivery_cli.py tests/unit/test_delivery_import_boundaries.py tests/unit/test_delivery_flow.py tests/unit/test_delivery_github_adapter.py -q
uv run ruff check apps/code_review_pipeline/delivery app/cli.py tests/unit/test_delivery_workspace.py tests/unit/test_delivery_cli.py tests/unit/test_delivery_import_boundaries.py
git diff --check
```

Expected: all pass; Ruff and whitespace checks exit 0.

- [ ] **Step 6: Run the complete unit and demo regression**

Run:

```powershell
uv run pytest tests/unit -q
uv run python scripts/run_change_demo.py
uv run python -m app.cli --help
```

Expected: all unit tests pass, the change demo remains dry-run, and CLI help lists `delivery run`.

- [ ] **Step 7: Commit the entry path**

```powershell
git add apps/code_review_pipeline/delivery/workspace.py apps/code_review_pipeline/delivery/__init__.py app/cli.py tests/unit/test_delivery_workspace.py tests/unit/test_delivery_cli.py tests/unit/test_delivery_import_boundaries.py
git commit -m "feat: add governed delivery cli entry"
```

- [ ] **Step 8: Perform the real S0 acceptance run manually**

Use a disposable branch in the target repository and an operator-controlled token.

```powershell
$env:CHANGE_AGENT_PUSH_ENABLED="1"
$env:CHANGE_AGENT_MODEL="nvidia/nemotron-3-super-120b-a12b"
$env:CHANGE_AGENT_VERIFY_COMMAND="uv run pytest tests/unit -q"
$baseSha = (git rev-parse origin/main).Trim()
$branch = "moa/delivery-smoke-$(Get-Date -Format yyyyMMddHHmmss)"
uv run python -m app.cli delivery run --repo shing26/moa-gateway --base-sha $baseSha --branch $branch --title "test: governed delivery smoke" --goal "add docs/delivery-smoke.md with one line stating that this file verifies the governed delivery path"
```

Expected:

- the command prints one `run_id`, the exact bundle hash, and asks for typed approval;
- before approval, the repository has no new branch;
- after approval, exactly one branch, one commit, and one PR exist;
- `data/delivery-runs/<run_id>.json` contains the same `bundle_hash` and `head_sha`;
- the merge store contains `shing26/moa-gateway#<pr>@<head_sha>` in `watching`;
- the existing merge worker can advance that record after CI becomes green;
- no CI-failure repair is attempted.

## Self-Review Checklist

- [ ] Spec §5 contracts all exist in `models.py`, including `schema_version`, immutable dataclasses, and `to_dict()`.
- [ ] Spec §6 outer states and legal transitions all exist in `state.py`.
- [ ] Spec §7 failure codes are represented by the enum; expected failures never become exceptions.
- [ ] Spec §8 reuse points are honored: `ChangePipeline`, `ChangeBundle`, existing tool loop, `GitHubClient`, `MergeStore`, `MergePoller`, `MergeExecutor`, and audit WAL.
- [ ] Spec §9 flow order is explicit: implement, prepare, optional same-quota revision, approval, publish, merge registration.
- [ ] Spec §10 tests cover serialization, illegal transitions, one revision, no write before approval, changed workspace, direct merge registration, and end-to-end fakes.
- [ ] No S0 task implements a Verifier, PR score, RAG, MCP, dashboard, queue, new database table, force push, or CI auto-fix.
- [ ] Every task has an exact test file, failing command, expected failure, implementation, passing command, and commit.
