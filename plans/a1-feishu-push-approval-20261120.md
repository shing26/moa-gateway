# Contract — A1 飞书远程推送审批（feishu-push-approval）

状态：**已冻结**（2026-11-20，所有者书面决策：通道=飞书；公网地址可配置归 moa-gateway）。
冻结后实现偏离须回到主会话重新冻结。
裁决方：主会话（审计）；实现方：独立 worktree Implementer；验证方：全新 verifier subagent。

## 修正案 1（2026-11-20，独立 Verifier 判 FAIL 后重新冻结）

Verifier 在 HEAD `11d8dd2` 上发现契约自身存在冲突，现按裁决修正，**以下条目取代原文**：

1. **`ChangeApproval.trace_id` 必须等于 `bundle.trace_id`**，不是 `hitl_id`。
   既有 `DeliveryFlow`（`flow.py:392`）与 `ChangePublisher`（`pipeline.py:423`）都以
   `approval.trace_id == bundle.trace_id` 作为进入推送的绑定条件；原文写 `hitl_id`
   会使飞书批准后必然被拒。`hitl_id` 只作为 pending 记录与 decision key 的关联标识，
   不进入 `ChangeApproval.trace_id`。
2. **approved 审计行的 `bundle_hash`**：允许修改
   `apps/code_review_pipeline/delivery/audit.py`，在 `run.bundle` 存在时把
   `bundle_hash` 并入 `extra`。这是审计层的最小改动，不改 `DeliveryFlow`。
3. **`delivery resume` 端口选择属于本契约范围**：恢复路径必须与原运行使用同一审批通道，
   否则远程审批会在 resume 时退回终端。`app/cli.py` 的范围扩为
   “`delivery run` 与 `delivery resume` 的端口选择及来源提示行”。
4. **路线图变更范围**：`docs/roadmap-generality-20261117.md` 的 2026-11-20 所有者决策
   记录（A1 通道、第二仓库、OTel、阶段表述）属于裁决方记录，纳入本契约范围；
   Implementer 不得再改其他行。
5. **A11 必补测试**（Verifier 指出的覆盖缺口）：
   - flow 级 A9：用带非空 `trace_id` 的真实 `ChangeBundle` 走批准 → 断言进入
     `PUSHING` 且 publisher 被调用；`trace_id` 或 `bundle_hash` 不符 → 拒绝；
   - 真并发回调：两个 callback 协程同时到达，只有一个写 decision；
   - insecure 分支：`insecure_self_approval` 与 `insecure_operator_unverified` 两个 gate
     字符串各有断言；
   - `delivery resume` 使用同一端口选择（开关开 → 飞书，关 → 终端）。

## 为什么

A2 已交付（2026-11-19）：审批身份可验证、approver ≠ operator、fail-closed 并写审计。
但**推送审批**仍只有 `TerminalPushApprovalPort`（`app/cli.py:312`）：

- 非 TTY / 无 stdin / 服务器 / CI 上一律拒绝；
- 第三个人不能在终端外批准——这是「别人能用」的唯一硬阻塞；
- 合并审批的飞书回程已存在（`app/routes/github_delivery.py` + `MergePoller` +
  `_handle_merge_approval`），缺的是 **PR 创建之前**的推送审批。

地址归属已解除：`laptop-todom8hg.tailcdb1fe.ts.net` 可配置为指向 moa-gateway 的监听端口。

## 设计

### 1. 端口与语义

新增 `FeishuPushApprovalPort`，实现既有 `PushApprovalPort` 协议
（`apps/code_review_pipeline/delivery/ports.py:70`）：

```python
async def request(self, *, run_id: str, bundle: ChangeBundle) -> ChangeApproval | None
```

**不新增状态、不改返回类型**：端口在 `READY_FOR_PUSH_APPROVAL`（`flow.py` 已落盘）阻塞等待
人工决定，默认上限 1800s、2s 轮询；批准返回 `ChangeApproval`，拒绝/超时/不可用返回 `None`
——既有 flow 把它落 `REJECTED`（`APPROVAL_REJECTED`，语义为「人拒绝或不可用」）。
`DeliveryFlow`、FSM 转移表、合并闸门零改动。

### 2. 跨进程挂起与恢复

CLI 与飞书回调服务是两个进程，共享 Redis：

1. CLI 在发卡前把 pending 记录写入 `SessionStore.store_hitl`
   （复用 `HitlRequest`；新增 `hitl_kind="push_approval"` 与可选
   `delivery_payload: dict` 字段，默认空、旧记录可反序列化）；
2. CLI 发送飞书审批卡，随后轮询 **decision key**（与 pending 记录同 TTL 量级）；
3. 飞书回调校验通过后用 `pop_hitl` **原子认领** pending（连点/并发只一个生效），
   再写 decision key（`approve` / `reject` + approver + channel + gate）；
4. CLI 读到 decision 后构造 `ChangeApproval` 并继续推送。

**Redis 降级为进程内存时拒绝启用本通道**（`using_memory=True` → 发卡前直接失败，
不得静默等待）：跨进程看不见的审批等于没有审批。

### 3. 卡片内容

`run_id`、`repo`、`branch`、`bundle_hash`、`patch_hash`、验证退出码、变更文件数、
过期时间；按钮：`批准推送` / `拒绝`。卡片正文不得包含 diff 内容、token 或密钥。

### 4. 回调校验（顺序固定）

1. 飞书验签 / `FEISHU_VERIFICATION_TOKEN`（既有 fail-closed，空 token 一律 401，
   除显式 `GATEWAY_ALLOW_INSECURE=1`）；
2. approver ∈ `HITL_APPROVER_IDS`（复用 `approver_gate_error`，空白名单 fail-closed）；
3. `pop_hitl` 原子认领；
4. **bundle 绑定**：回读 `delivery_run_store.get(run_id)`，其
   `run.bundle.bundle_hash` 必须等于 pending 记录中的 `bundle_hash`；不等 → 拒绝
   （不得只信卡片上写的值）；
5. **自审批**：配置了 `FEISHU_OPERATOR_OPEN_ID` 时 approver == operator → 拒绝
   （`self_approval`）；未配置 → 拒绝（`operator_unverified`），除非显式
   `GATEWAY_ALLOW_INSECURE=1`（审计标记 `insecure_*`）；
6. 写 decision key；回调只记录决定，**不在服务器进程里推送**。

### 5. 批准后的审批对象

```python
ChangeApproval(
    approver=<飞书 open_id>,
    trace_id=<bundle.trace_id>,   # 修正案 1：不是 hitl_id
    bundle_hash=bundle.bundle_hash,
    run_id=run_id,
    channel="feishu",
    identity_verified=True,
    operator_login=<GITHUB_TOKEN 主人，get_authenticated_login()>,
    gate="allowlist" | "insecure" | "insecure_self_approval" | ...,
)
```

推送仍由操作者机器上的 CLI 用其 PAT 执行，仍受既有 `ChangePublisher` 约束
（建分支/commit/PR；禁止 force push 与删分支）。`get_authenticated_login()` 失败
上抛 → flow 映射 `INFRA_ERROR`，不得吞成拒绝或放行。

### 6. 审计

`approved` 行带 `approver` / `channel` / `identity_verified` / `operator_login` / `gate`，
并带 `bundle_hash`；拒绝、超时、bundle 不符、身份不符均写审计（含稳定原因码）。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `FEISHU_PUSH_APPROVAL_ENABLED` | `0` | 开：CLI 使用飞书端口；关：终端端口（逐字回归） |
| `FEISHU_PUSH_APPROVAL_TIMEOUT_S` | `1800` | 等待上限（秒）；超时 → `None` |
| `FEISHU_OPERATOR_OPEN_ID` | 空 | 操作者自己的飞书 open_id；空 = fail-closed（除显式 insecure） |
| `DELIVERY_APPROVAL_CHAT_ID` | 回退 `CODE_REVIEW_MERGE_CHANNEL` | 卡片目标会话 |

复用 `HITL_APPROVER_IDS`、`FEISHU_APP_ID` / `FEISHU_APP_SECRET`、
`FEISHU_VERIFICATION_TOKEN`、`REDIS_URL`。

## Allowed paths

```
apps/code_review_pipeline/delivery/feishu_approval.py   (新文件：端口、发卡、轮询、decision key)
app/engine.py                                           (只加 HitlRequest.delivery_payload 可选字段)
app/routes/feishu.py                                    (只加 push_approval 回调分支与校验)
app/cli.py                                              (delivery run 与 resume 的端口选择及来源提示行)
app/config.py                                           (只加上述 4 个设置)
app/deps.py                                             (只在需要时注入共享 SessionStore)
apps/code_review_pipeline/delivery/audit.py             (修正案 1：approved 行补 bundle_hash)
tests/unit/**                                           (新增/修改回归测试)
tests/integration/**                                    (Redis 跨进程语义，可自跳过)
docs/adr/adr-042-feishu-push-approval.md                (新 ADR：为什么阻塞等待而非新增状态、fail-closed 姿态)
docs/roadmap-generality-20261117.md                     (只承载 2026-11-20 决策记录与 A1 行状态)
.env.template                                           (只加新变量与注释)
plans/a1-feishu-push-approval-20261120.md               (本文件)
```

## Acceptance criteria

- **A1 双态**：开关关：`delivery run` 行为与今天逐字一致（终端端口、非 TTY/EOF 拒绝路径
  不变）；开关开：使用飞书端口，非 TTY 也能进入等待。
- **A2 发卡**：进入 `READY_FOR_PUSH_APPROVAL` 后发出含 run_id / bundle_hash / patch_hash /
  branch / verify exit 的审批卡；卡片不含 diff、token、密钥。
- **A3 白名单**：approver 不在 `HITL_APPROVER_IDS` → 拒绝且不写 decision；
  空白名单且无显式 insecure → 拒绝。
- **A4 自审批**：`FEISHU_OPERATOR_OPEN_ID` 已配置且 approver 相同 → 拒绝
  （`self_approval`）；未配置 → 拒绝（`operator_unverified`）；显式 insecure 放行时
  审计 `gate` 如实标记。
- **A5 bundle 绑定**：pending/卡片中的 `bundle_hash` 与 run store 中的当前 bundle 不等
  → 拒绝，审计留证；相等才允许批准。
- **A6 幂等**：同一卡片连点两次或并发回调，只有一个生效（`pop_hitl` 原子认领）；
  第二次得到「已失效或已被处理」。
- **A7 超时**：超过 `FEISHU_PUSH_APPROVAL_TIMEOUT_S` 无决定 → `request()` 返回 `None`，
  run 落 `REJECTED`，不推送；卡片侧提示过期。
- **A8 Redis fail-closed**：HITL 存储处于进程内存降级（`using_memory=True`）时，
  开关开着也拒绝发卡并给出明确原因；不得静默等待到超时。
- **A9 批准路径**：批准后 `ChangeApproval` 的 `channel="feishu"`、
  `identity_verified=True`、`approver=<open_id>`、`operator_login=<token 主人>`、
  `gate` 正确；推送仍走既有 bundle 校验与 PR 创建；审计 `approved` 行字段完整。
- **A10 验签不回归**：`FEISHU_VERIFICATION_TOKEN` 为空时回调仍 401（除显式 insecure）；
  加密事件仍 400。
- **A11 测试**：新增覆盖 A1–A10 的单测（含真并发回调、bundle/trace_id 不符、身份不符、
  超时、insecure 两个 gate、flow 级 trace_id 绑定、resume 端口选择）；
  既有单测全绿，只允许在既有测试文件末尾追加，不改既有断言。
- **A12 证据绑定**：HEAD SHA + `git status --short`。

## Verification commands

```
V1  PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/unit/test_feishu_push_approval.py -q
V2  PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/unit/test_feishu_route.py tests/unit/test_delivery_cli.py -q
V3  PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/integration -q -ra
V4  PYTHONPATH=. .venv/Scripts/python.exe -m ruff check .
V5  PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/unit -q
```

V1 文件名以实现实际创建为准，PR 里写清。真实飞书端到端审批属于合并后的运维动作。

## 合并后的部署动作（主会话执行并留证）

1. 公网地址（funnel）指向 moa-gateway 监听端口，确认 `/webhook/callback` 可达；
2. 配置 `FEISHU_VERIFICATION_TOKEN`、`HITL_APPROVER_IDS`、`FEISHU_OPERATOR_OPEN_ID`、
   `DELIVERY_APPROVAL_CHAT_ID`、`FEISHU_PUSH_APPROVAL_ENABLED=1`；
3. 配置 `DELIVERY_WEBHOOK_ENABLED=1` 与 GitHub 仓库 webhook（`pull_request`、
   `check_suite`）；
4. 阶段门槛演示：第二身份在飞书批准一次推送 → CLI 推送 → PR → CI 绿 → 合并审批 →
   合并；自审批被拒且有审计记录；台账入下一行（注明「A1 远程审批验收」）。

## Forbidden

- 改 `DeliveryFlow` 返回类型、`ChangeApproval` 既有字段语义、FSM 转移表、合并闸门；
- 在服务器进程里执行推送（推送必须由操作者机器上的 CLI 用其 PAT 执行）；
- 空白名单放过、自审批放过（除显式 insecure 且审计如实标记）；
- 只信卡片上的 bundle_hash 而不回读 run store；
- 支持飞书加密事件（仍明确 400）；
- 顺手改终端审批端口、既有飞书聊天 HITL、PR 审查通道。

## 已知边界（不在本契约）

- 单机假设：`delivery_run_store` 是本地目录，CLI 与回调服务需在同一文件系统；
  跨机需共享存储，另立契约。
- 阻塞等待期间 CLI 进程被占用（最长 timeout）；进程被杀 → run 停在
  `READY_FOR_PUSH_APPROVAL`，pending 记录按 TTL 失效，需重新 `delivery resume`。
- 强模型通过率、探索/写入预算分离属于 B 阶段，不在本契约。

## 签收记录（2026-11-20）

**所有者书面签收**：用户消息「签收 PR #112」。

**合并**：PR #112 → `main`，merge commit
`3177080854f387c91fcdf04d208abec117607481`，2026-11-20T05:54:39Z。

**验收证据**

- 实现 HEAD：`5a63d79edb0f6b680148e13d7b569796e97963d8`；`git status --short` 空。
- 独立 Verifier 在实现 HEAD 上亲跑：V1 22 passed；V2 42 passed；V3 37 passed /
  16 skipped（缺 `CODE_REVIEW_DATABASE_URL`，按设计自跳过）；V4 ruff clean；
  V5 2239 passed / 6 skipped；A1–A12 全 PASS，无 P0/P1。
- GitHub CI：`test` SUCCESS、`integration` SUCCESS、`docker` SKIPPED。

**人审四问**

1. **是否在冻结契约范围内？** 是。13 个变更文件全部落在修正案 1 的 Allowed paths；
   `flow.py` / `pipeline.py` / 状态机 / 合并闸门 / 终端端口 / 聊天 HITL / PR 审查通道零改动。
2. **证据是否绑定 HEAD 且可复跑？** 是。HEAD SHA + 干净状态 + V1–V5 命令与计数；
   Verifier 独立重跑，不采信实现方自评。
3. **是否有未声明的行为变化？** 无。修正案 1 已显式记录 `trace_id` 绑定、
   `audit.py` 补 `bundle_hash`、`delivery resume` 端口、路线图决策记录四处范围调整。
4. **残余是否如实记录并接受？** 是。P2 三项（`audit.py` 分支无直接单测、超时后卡片不更新、
   白名单拒绝消息在缺 `chat_id` 时可能送不达）不阻断，已在 PR 与本节留痕。

**后续（不在本次签收范围）**

- 部署：funnel → moa-gateway、飞书与 GitHub webhook、`FEISHU_PUSH_APPROVAL_ENABLED=1`。
- 阶段门槛：第二身份在飞书批准一次推送 → PR → CI → 合并；台账入下一行。
