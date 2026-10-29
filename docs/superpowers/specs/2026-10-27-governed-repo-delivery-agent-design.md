# 受治理仓库交付 Agent · 设计

- 日期：2026-10-27
- 状态：已确认
- 关联：ADR-021（审批合并）、ADR-023（Agent 原语）、ADR-027（工作区写隔离）、
  ADR-030（受治理变更 agent）
- 落点：`apps/code_review_pipeline/delivery/`、`apps/code_review_pipeline/change/`、
  `apps/code_review_pipeline/routing/github_client.py`

## 1. 背景

仓库已有三块可复用能力，但它们还没有组成一个完整的产品闭环：

1. `app/agents/workspace_tools.py` 已提供文件读写、搜索和固定验证命令；
2. 变更写侧已有验证、bundle 哈希、人工审批、分支、commit 和 PR 原语；
3. 合并通道已有 CI 轮询、审批卡、head SHA 乐观锁和合并状态机。

缺口不在“再做一个 Agent 框架”，而在以下接缝：

- `ChangePipeline` 只有 demo 和测试调用，没有产品入口；
- `ChangeAgent` 只是一个 `run(workspace, goal) -> str` 协议，没有真实实现；
- Verifier 只有实验台里的 L1 Critic 原型，没有产品职责和权限隔离；
- `create_pull_request` 只返回 URL，不返回 PR number，无法直接登记 merge record；
- 没有外层 `DeliveryRun` 把编码、验证、审批、推送、CI 和合并证据串起来。

本设计把现有能力收敛成一个产品级深模块：

```text
DeliveryRequest -> DeliveryRun
```

产品只解决一条链路：目标或失败信息 → 隔离工作区改代码 → 确定性验证 →
可选只读 Verifier → 人工推送审批 → PR → CI → 人工合并审批 → SHA 锁合并。

## 2. 目标

### 2.1 产品目标

- 用一个单 Agent 基线完成真实仓库的“改代码 → 验证 → PR → CI → 审批合并”闭环。
- 所有外部副作用仍经过现有 bundle、审批、审计和合并状态机。
- 产品入口不暴露内部 Agent、工具、GitHub client 或 merge store。
- 默认拓扑为 `single`；`verified` 只作为预注册实验，不默认开启。

### 2.2 工程目标

- Implementer 与 Verifier 使用独立会话和不同工具权限。
- Verifier 物理上不能写文件、审批或合并。
- 最多一个修复周期，不让 Critic 回边无限循环。
- 同一 bundle hash 对应同一审批和同一推送；同一 head SHA 对应同一合并许可。
- 所有 Agent 成本和工具轨迹进入同一份 `DeliveryRun`。

### 2.3 实验目标

- 在隐藏验收集上比较 `S0`（单 Agent）与 `S1`（Implementer + 一次只读 Verifier）。
- S1 的价值不是“多说一轮”，而是减少“可见测试通过、隐藏验收失败”的错误放行。
- 没有预注册证据时，S1 不进入默认产品路径。

## 3. 非目标

- 不做 PR / Issue / Fork 的“正向 / 负向”评分。
- 不做 L2 Planner、L3 并行 worker 或通用 Supervisor。
- 不做 CI 自动修复；CI 红仍进入人工处理。
- 不做新的队列、dashboard、渠道、RAG 或 MCP 入口。
- 不引入 Redis/Postgres checkpointer。
- 不支持多租户、多用户或多仓库批处理。
- 不让任何 Agent 直接持有 GitHub token、审批权限或合并权限。
- 不让产品核心 import `bench/`。

## 4. 核心决策

### 4.1 一个深模块，两个 Agent 角色

外部只看到 `DeliveryFlow.run(request) -> DeliveryRun`。
内部只有两个 Agent：

| 角色 | 目标 | 工具权限 | 禁止项 |
| --- | --- | --- | --- |
| Implementer | 修改工作区使目标达成 | `read_file` / `search` / `write_file` / `run_tests` | GitHub、审批、合并 |
| Verifier | 独立复核当前 bundle 是否真的达成目标 | `read_file` / `search` / `run_tests` | `write_file`、GitHub、审批、合并 |

协调、状态推进、预算合并由 `DeliveryFlow` 负责，不是第三个 LLM Agent。

### 4.2 Verifier 是建议者，不是判决者

- Verifier 的 `approved` 只允许进入人工推送审批。
- 测试和 CI 仍是确定性判决。
- 人工审批仍是外部动作的唯一授权。
- Verifier 输出损坏、缺证据或调用失败时 fail-closed，不能静默退化成 S0。

### 4.3 默认 S0，S1 预注册

- `DeliveryPolicy.topology` 默认 `single`。
- `verified` 必须显式配置并由实验协议选择。
- S1 使用同一个入口、同一套 bundle、审批和合并逻辑；差异只在是否有只读 Verifier。

### 4.4 不把 Verifier 塞进 `ChangePipeline`

`ChangePipeline` 继续负责变更准备、审批和远端写侧；`DeliveryFlow` 负责 Agent 编排。
为避免同一 bundle 被生成两次，将现有 `ChangePipeline` 内部能力拆成：

- `prepare_bundle(...)`：运行确定性验证并生成 `ChangeBundle`，零远端副作用；
- `publish_bundle(bundle, approval)`：校验审批并执行建分支、commit、PR；
- 现有 `ChangePipeline.run(...)` 保留为兼容 facade，组合上述两步。

Verifier 必须审阅最终即将发布的同一个 bundle。若审阅后工作区或 bundle hash 改变，
本次审批与 Verifier 结论同时失效。

## 5. 数据契约

所有产品类型使用不可变 dataclass，并通过唯一的 `to_dict()` 出口序列化。
`DeliveryRun` 带 `schema_version`，新字段不得改变既有字段语义。

### 5.1 DeliveryRequest

```python
DeliveryOrigin = Literal["manual", "issue", "ci_failure"]

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
```

约束：

- `repo` 必须是 `owner/name`，且与 pipeline 配置完全一致。
- `base_sha` 必须是远端真实 commit。
- `trace_id` 非空；`branch` 非空。
- 验证命令、模型、预算、拓扑和 token 不在请求里，由部署方策略注入。
- `source_ref` 只保存 issue/CI 引用，不把完整日志复制进请求。

### 5.2 DeliveryPolicy

```python
@dataclass(frozen=True)
class DeliveryPolicy:
    verify_command: tuple[str, ...]
    topology: Literal["single", "verified"] = "single"
    max_revisions: int = 1
    max_steps: int = 40
    max_tokens: int = 400_000
```

约束：

- `max_revisions` 固定为 1；出现其他值视为配置错误。
- `max_steps` 与 `max_tokens` 是整条 `DeliveryRun` 的硬上限；修复轮和 Verifier
  共用剩余额度，不按角色各发一份。
- `topology=verified` 时 Verifier 必须存在；不存在时直接失败。
- 策略由部署配置或实验 harness 提供，不能由模型修改。

### 5.3 AgentAttempt 与执行账本

```python
AgentRole = Literal["implementer", "verifier"]
AgentStatus = Literal["completed", "step_limit", "budget_exhausted", "error"]

@dataclass(frozen=True)
class AgentStep:
    index: int
    action: Literal["call_tool", "finish"]
    tool: str
    arguments: dict[str, Any]
    observation: str
    outcome: Literal["ok", "rejected", "failed", ""]
    elapsed_ms: float

@dataclass(frozen=True)
class DeliveryUsage:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_latency_ms: float = 0.0

@dataclass(frozen=True)
class AgentAttempt:
    role: AgentRole
    status: AgentStatus
    summary: str
    stop_reason: str
    usage: DeliveryUsage
    steps: tuple[AgentStep, ...]
```

`AgentAttempt` 是执行体自报，不是判决。`completed` 只表示模型认为本轮完成，
不表示验证通过、Verifier 认可或允许推送。

### 5.4 VerifierVerdict

```python
@dataclass(frozen=True)
class EvidenceRef:
    kind: Literal["file", "command", "diff"]
    ref: str
    observation: str

@dataclass(frozen=True)
class VerifierVerdict:
    decision: Literal["approved", "revise"]
    reason: str
    instructions: str
    evidence: tuple[EvidenceRef, ...]
    reviewed_bundle_hash: str
    reviewed_patch_hash: str
    attempt: AgentAttempt
```

约束：

- `approved` 至少包含一条证据。
- `revise` 必须包含非空且可执行的 `instructions`。
- `reviewed_bundle_hash` 与 `reviewed_patch_hash` 必须匹配当前 bundle。
- 解析不出结构、缺少证据或引用旧 bundle 时，结果为 `VERIFIER_INVALID`。
- Verifier 不产生 `pass/fail` 判决，只产生 `approved/revise` 建议。

### 5.5 PushOutcome 与 PullRequestRef

```python
@dataclass(frozen=True)
class PullRequestRef:
    number: int
    url: str
    head_sha: str

@dataclass(frozen=True)
class PushOutcome:
    branch: str
    commit_sha: str
    pull_request: PullRequestRef
```

`GitHubClient.create_pull_request` 改为返回 `PullRequestRef`。
`create_commit` 返回的 commit SHA 必须被保留，不能像当前实现一样丢弃。

### 5.6 DeliveryRun

```python
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

@dataclass(frozen=True)
class VerificationEvidence:
    command: tuple[str, ...]
    exit_code: int
    output_hash: str
    output_excerpt: str

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

@dataclass(frozen=True)
class DeliveryRun:
    schema_version: int
    run_id: str
    trace_id: str
    request: DeliveryRequest
    topology: Literal["single", "verified"]
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
```

约束：

- `run_id` 与 `trace_id` 分离；重跑产生新 `run_id`，不覆盖旧 Run Record。
- `bundle` 直接使用现有 `ChangeBundle`。
- `merge` 直接引用现有 `MergeRecord`，不复制 CI / 合并状态。
- `usage` 汇总 Implementer、Verifier 和协调开销。
- `to_dict()` 是唯一序列化出口；patch 展示可截断，但哈希和验证输出摘要不可截断。

第一版不新增数据库表。完整 Run Record 以 JSON 写入 `data/delivery-runs/`，
审计 WAL 写入紧凑状态迁移与角色摘要。现有 merge store 继续作为 PR 后状态的权威来源。
`run_id` 对应的 JSON 保存创建 PR 时的快照；读取当前视图时，按
`repo + pr_number + head_sha` 合并最新 `MergeRecord`，不复制第二份 CI / 合并状态。

## 6. 状态机

外层使用 `DeliveryRunState`。现有 `ChangeStatus` 与 `MergeState` 保留为内部适配器。

| 状态 | 允许的下一步 |
| --- | --- |
| `PREPARING` | `IMPLEMENTING`、`NEEDS_HUMAN` |
| `IMPLEMENTING` | `CHECKING`、`BUDGET_EXHAUSTED`、`FAILED` |
| `CHECKING` | `VERIFIER_REVIEW`、`READY_FOR_PUSH_APPROVAL`、`REVISING`、`NEEDS_HUMAN`、`BUDGET_EXHAUSTED` |
| `VERIFIER_REVIEW` | `READY_FOR_PUSH_APPROVAL`、`REVISING`、`NEEDS_HUMAN`、`BUDGET_EXHAUSTED` |
| `REVISING` | `CHECKING`、`NEEDS_HUMAN`、`BUDGET_EXHAUSTED` |
| `READY_FOR_PUSH_APPROVAL` | `PUSHING`、`REJECTED` |
| `PUSHING` | `WAITING_CI`、`FAILED` |
| `WAITING_CI` | `AWAITING_MERGE_APPROVAL`、`NEEDS_HUMAN` |
| `AWAITING_MERGE_APPROVAL` | `MERGING`、`REJECTED`、`NEEDS_HUMAN` |
| `MERGING` | `MERGED`、`NEEDS_HUMAN` |
| `MERGED` | 终态 |
| `REJECTED` | 终态 |
| `NEEDS_HUMAN` | 终态 |
| `BUDGET_EXHAUSTED` | 终态 |
| `FAILED` | 终态 |

### 6.1 有界回边

```text
Implementer
  -> CHECKING
      -> 测试失败：REVISING 一次
      -> 测试通过 + S1：VERIFIER_REVIEW
          -> APPROVED：READY_FOR_PUSH_APPROVAL
          -> REVISE：REVISING 一次
          -> 第二次仍为 REVISE：NEEDS_HUMAN
```

测试失败和 Verifier 打回共用同一个修复额度，不各送一次。修复后必须重新运行完整
验证命令，再进入最多一次 Verifier 复核。不存在第三次修改。

## 7. 失败语义

| FailureCode | 含义 |
| --- | --- |
| `NO_CHANGES` | Implementer 没有产生文件改动 |
| `VERIFY_FAILED` | 最终确定性验证仍非零 |
| `VERIFIER_INVALID` | Verifier 输出不可解析、缺证据或引用旧 bundle |
| `VERIFIER_REVISE_EXHAUSTED` | 修复额度用尽后仍未获批准 |
| `BUNDLE_CHANGED` | 审批或 Verifier 后工作区内容发生变化 |
| `APPROVAL_REJECTED` | 人拒绝推送或合并，正常终态 |
| `PUSH_FAILED` | GitHub 写侧失败 |
| `CI_FAILED` | CI 红，转人工；本版不自动修复 |
| `MERGE_SHA_MISMATCH` | 审批后的 head SHA 已变化 |
| `MERGE_NOT_MERGEABLE` | PR 冲突或被保护分支拒绝 |
| `INFRA_ERROR` | 网络、存储或配置错误 |

失败语义约束：

- `BUDGET_EXHAUSTED` 与 `VERIFY_FAILED` 分开。
- `APPROVAL_REJECTED` 不写成系统错误。
- 任何失败都不能 force push、删除分支、自动重试合并或绕过 branch protection。
- `CI_FAILED` 后 merge record 保持可恢复；CI 重跑变绿时现有 merge poller 仍可推进。

## 8. 现有模块接缝

| 模块 | 用途 | 约束 |
| --- | --- | --- |
| `workspace_tools.build_toolset` | 构造 Implementer / Verifier 工具集 | Verifier names 不含 `write_file` |
| `ChangeBundle` / `build_change_bundle` | 唯一代码快照与哈希语义 | 纯数据，零远端副作用 |
| `ChangePipeline` | 兼容 facade；内部拆成 prepare / publish | 远端写只在 publish |
| `GitHubClient.create_pull_request` | 返回 `PullRequestRef` | 必须包含 PR number 与 head SHA |
| `GitHubClient.create_commit` | 返回 commit SHA | SHA 必须写入 `PushOutcome` |
| `MergeStore.save` | PR 创建后直接登记 merge record | 不依赖 webhook 才进入轮询 |
| `MergePoller` / `MergeExecutor` | CI、审批、SHA 锁合并 | 状态机保持原样 |
| `task_audit.record_human_decision` | 审批审计 | 不新建第二条审计链 |
| `app/agents/stubs.py` 工具循环 | 生产 Implementer / Verifier adapter 的唯一循环来源 | 扩展为可注入工具集并返回 `AgentAttempt` |
| `bench.agent.LLMBackedAgent` | 仅实验 adapter 的实现来源 | 产品核心不得 import `bench` |

### 8.1 依赖方向

```text
delivery/
  models.py       # DeliveryRequest / DeliveryRun / VerifierVerdict
  ports.py        # ImplementerPort / VerifierPort / ChangePublisher
  flow.py         # DeliveryFlow

delivery/adapters/
  production_agent.py  # 复用 app/agents/stubs.py 工具循环，不依赖 bench
  bench_agent.py       # S1 实验专用 adapter，把 bench AgentRun 映射成 AgentAttempt
  github.py       # 把 ChangePublisher 接到 ChangePipeline / GitHubClient
```

端口签名：

```python
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

class PushApprovalPort(Protocol):
    async def request(self, bundle: ChangeBundle) -> ChangeApproval | None: ...

class ChangePublisher(Protocol):
    async def publish(
        self,
        *,
        workspace: Path,
        bundle: ChangeBundle,
        approval: ChangeApproval,
    ) -> PushOutcome: ...
```

`DeliveryFlow` 在构造期接收这些端口与 `DeliveryPolicy`，公开入口仍只有
`run(request) -> DeliveryRun`。人工推送审批通过 `PushApprovalPort` 请求；返回 `None`
表示拒绝，进入 `REJECTED`。这样不会为 CLI 和未来回调入口各写一套交付流程。

产品核心只依赖 `ports.py` 中的协议。真实 LLM、GitHub 和存储都是 adapter，
测试使用内存 fake。一个 adapter 不构成 seam；这里至少有真实 adapter 与 fake adapter
两个实现，因此协议是真实 seam。

生产 adapter 复用 `app/agents/stubs.py` 已有工具循环，不新写第三套 Agent 循环。
该循环需要做一次有界接口扩展：允许注入显式工具集（可写或只读），并返回结构化的
`AgentAttempt`。S1 实验可以使用独立的 `bench_agent.py` 复用 `bench/` 原语，
但该 adapter 不能被 `delivery/flow.py` 直接 import。

## 9. 数据流

```text
DeliveryRequest
  -> DeliveryFlow 解析 repo / base SHA / policy
  -> ImplementerPort.run(workspace, goal)
  -> prepare_bundle()（确定性验证 + ChangeBundle）
  -> 若失败且仍有修复额度：ImplementerPort.run(feedback)
  -> 若 topology=verified：
       VerifierPort.review(workspace, goal, bundle, attempt)
       -> revise：ImplementerPort.run(instructions)
       -> 重新 prepare_bundle()
       -> 最多再审一次
  -> READY_FOR_PUSH_APPROVAL
  -> ChangeApproval(trace_id, bundle_hash, approver)
  -> publish_bundle()
  -> Git branch / commit / PR
  -> MergeStore.save(MergeRecord)
  -> MergePoller
  -> AWAITING_MERGE_APPROVAL
  -> MergeExecutor(head_sha)
  -> MERGED
```

`DeliveryFlow` 返回的 `DeliveryRun` 可以停在 `READY_FOR_PUSH_APPROVAL` 或 `WAITING_CI`。
后续推进由现有审批回调与 merge worker 完成，不新增第二套调度器。

## 10. 测试面

测试只穿过 `DeliveryRequest -> DeliveryRun` 与端口协议，使用 fake Implementer、
fake Verifier、fake publisher 和内存 MergeStore。

### 10.1 单元与契约

- `DeliveryRequest`、`DeliveryPolicy`、`DeliveryRun` 的不可变性与序列化。
- `schema_version`、枚举值和 `to_dict()` 字段覆盖。
- 非法 repo、空 trace、空 branch、非远端 base SHA 的 fail-closed。
- `bundle_hash` 变化会让旧 approval 和旧 VerifierVerdict 同时失效。

### 10.2 权限

- Verifier 工具集没有 `write_file`。
- Verifier 尝试写文件时被拒绝，文件哈希不变。
- Verifier 没有 GitHub client、approval 或 merge executor 依赖。
- Implementer 和 Verifier 使用独立会话，不共享隐藏上下文。

### 10.3 有界编排

- 测试失败触发一次修复，修复后重新验证。
- 首次 `REVISE` 消耗唯一修复额度。
- 第二次 `REVISE` 进入 `NEEDS_HUMAN`，不再调用 Implementer。
- S1 Verifier 缺失或失败时不得退化成 S0。
- S0 不调用 Verifier，其余状态与 S1 一致。

### 10.4 副作用顺序

- 审批审计发生在任何远端写之前。
- 审批前没有 create_ref / create_commit / create_pull_request。
- PR 创建成功后直接登记 merge record。
- 审批后文件或 bundle 变化时不创建远端分支。
- HEAD SHA 变化时 merge 被拒绝。

### 10.5 端到端

- 用 fake GitHub 验证 `S0` 从改代码到 PR、CI 绿、审批、合并的状态链。
- 用 scripted Implementer / Verifier 验证 `S1` 一次 revise 后通过。
- 用 scripted Implementer / Verifier 验证两次 revise 后被挡住。
- 验证 merge record、audit、DeliveryRun 三者的 trace_id / run_id / bundle hash 一致。

## 11. S0 / S1 实验设计

### 11.1 变量

- `S0`：Implementer only。
- `S1`：Implementer + 一次只读 Verifier 回边。

固定条件：

- 同一模型、同一温度、同一验证命令。
- 同一总 token / step 预算；Verifier 消耗计入 S1 总量。
- 同一工作区物化与测试树还原规则。
- 预注册任务集、重复次数、排除规则与报告口径。

### 11.2 任务与判决

每个任务包含：

- 一个 fail-first 的可见测试集；
- 一个不被 Implementer / Verifier 看到的隐藏验收集；
- 一个明确的 base commit 与来源。

最终判决由隐藏验收集给出。Verifier 只能读工作区、可见测试和 implementer 轨迹，
不能看到隐藏验收集，也不能修改测试。

主要指标：

- 隐藏验收通过率；
- “可见测试通过但隐藏验收失败”的错误放行率；
- 每 pass 的 token；
- Verifier 打回后由 Implementer 修复并最终通过的比例；
- S1 相对 S0 的增量成本。

次要指标：

- 工具错误与参数拒绝次数；
- step_limit / budget_exhausted / error；
- 人工介入次数。

### 11.3 门禁

- pilot 基线必须落在可区分区间；贴 0% 或 100% 不启动正式批次。
- pilot 或正式批次出现无法归因的 `error` 时不得升级结论。
- 只有同一模型、同一协议、同一任务矩阵的数据才能比较 S0 / S1。
- S1 未证明降低错误放行率前，默认产品拓扑保持 S0。
- 旧 v5/v8 多智能体结论不能被复用为 S1 的增益证据。

## 12. 实施顺序

1. 新增 ADR-031，明确仓库交付 Agent 的产品边界、Verifier 权限和“不自动修复 CI”的边界。
2. 同步 `CONTEXT.md`、`delivery/定位与减法清单.md` 和 README，消除“开发辅助 Agent 不做产品”的冲突。
3. 实现 Delivery 数据契约、状态机、ports 与 fake adapters；此时无真实 LLM 和远端写入。
4. 将 `ChangePipeline` 内部拆成 prepare / publish，保持现有 facade 测试全绿。
5. 扩展生产工具循环、实现 S0 的 production agent adapter、GitHub adapter 与真实 PR
   闭环；默认 dry-run、推送显式开启。
6. 实现 Verifier adapter 与 S1 预注册 pilot；默认关闭。
7. 只有 S1 通过预注册门禁且确有增量价值，才讨论默认开启。

## 13. 验收标准

### 13.1 设计验收

- 外部只有一个产品入口概念：`DeliveryRequest -> DeliveryRun`。
- Verifier 的权限与 Implementer 不同，且不能审批或合并。
- 修复周期最多一次。
- S0 与 S1 共用同一数据契约、验证、bundle 和合并路径。
- CI 失败在 v1 只转人工，不产生自动修复承诺。
- 产品核心不依赖 `bench/`。

### 13.2 代码验收

- 变更管线与现有审批/bundle 安全测试全绿。
- Delivery 契约、状态机、权限和副作用顺序有回归测试。
- 真实 GitHub 写侧仍默认关闭，只有显式配置和人工审批才推送。
- PR 创建后无需 webhook 即可登记 merge record。

### 13.3 产品验收

- 在一个真实仓库上完成 S0 的：
  `目标 -> 工作区 -> 验证 -> PR -> CI -> 审批 -> 合并`。
- 该次运行的 `DeliveryRun`、审计 WAL 和 `MergeRecord` 可相互核对。
- 对外只宣称“受治理的仓库交付 Agent，S1 Verifier 为实验能力”，不宣称
  “自动合并”“CI 自动修复”或“多智能体有增益”。

## 14. 风险与边界

- GitHub token 仍是高价值单点；权限面必须限定在目标仓库。
- CI 失败日志的读取能力取决于 token 权限；权限不足必须如实转人工。
- 远端工作区物化、PR number 返回和 merge record 直连是本设计的前置缺口。
- S1 可能增加成本但没有提升隐藏验收通过率；该负结果同样是有效结论。
- 第一版 Run Record 使用 JSON 文件而非数据库；多实例共享不是当前目标。
