# 契约：推送审批「超时」与「拒绝」分离

- **task id**：`delivery-approval-timeout-20261123`
- **分支**：`fix/approval-timeout-semantics-20261123`
- **背景**：`PushApprovalPort.request` 只用 `None` 表达「没拿到批准」，
  `DeliveryFlow` 把 `None` 一律映射成 `REJECTED` / `approval_rejected`。
  2026-11-23 的 `dlv-ced2cc69a7984332acffc931b6a9ad8a` 实测：审批卡发出后
  无人点击，30 分钟到期，记录落成 `rejected` + `approval_rejected`，而
  `push` 为 `null`、没有创建任何 PR。超时不是人的决定；同时该状态被
  `delivery resume` 拒绝（resume 只收 `ready_for_push_approval`），
  一次「没看见卡片」就变成不可恢复的死单。

## 1. 语义

1. **拒绝**（人点了拒绝 / 门禁不可用 / 非 TTY / 非白名单 / 自审批被拒）：
   保持现状——`REJECTED` + `FailureCode.APPROVAL_REJECTED`，不可自动恢复。
2. **超时**（审批端口自己的等待窗口到期，未收到任何决定）：
   - 新增 `FailureCode.APPROVAL_TIMED_OUT = "approval_timed_out"`；
   - 状态**留在** `READY_FOR_PUSH_APPROVAL`（bundle 仍冻结有效）；
   - **不得**写 `rejected`、**不得**推送、**不得**把超时算作人的决定。
3. 超时后 `delivery resume --run-id <id>` 必须能重新进入同一个推送门禁；
   再次超时仍是超时，记录保持可恢复。
4. 除「端口自己的等待到期」外的任何异常（网络、存储、身份读取失败）
   仍是 `NEEDS_HUMAN` / `INFRA_ERROR`，不得伪装成超时。

## 2. 实现要求

1. `ports.py` 新增 `PushApprovalTimedOut(RuntimeError)`，作为超时的唯一信号。
2. `feishu_approval.py`：等待窗口到期时，先 `remove_hitl`、记 warning，
   再 `raise PushApprovalTimedOut(...)`；不得写 decision、不得返回 `None`。
   人工拒绝（decision 非 approve / bundle 不匹配 / 卡片发送失败）路径不变。
3. `flow.py`：在通用 `except Exception` **之前**捕获 `PushApprovalTimedOut`，
   把 `failure=FailureInfo(APPROVAL_TIMED_OUT, ...)` 写回同一个
   `READY_FOR_PUSH_APPROVAL` 记录并写审计；不得调用 `_finish(REJECTED)`。
   由于状态机不允许自转移，新增「原地记失败」的内部方法，不要改 `state.py`。
4. `resume.py`：`PushApprovalTimedOut` 不得被当成 `_ApprovalDenied`。
   捕获后持久化 `APPROVAL_TIMED_OUT`（状态不变），返回非零退出码并提示
   `delivery resume --run-id <id>`；不得 publish。
5. `app/cli.py::cmd_delivery_run` 退出码：仅当
   `state == waiting_ci`，或 `state == ready_for_push_approval 且 failure is None`
   时返回 0；`approval_timed_out` 必须返回非 0，并打印
   `resume    : delivery resume --run-id <id>`。
6. `delivery retry` 对 `approval_timed_out` 记录必须拒绝开新单
   （状态不在 `RETRY_STATES`，行为保持，不允许为此放行）。

## 3. 允许路径（Allowed paths）

```
apps/code_review_pipeline/delivery/ports.py
apps/code_review_pipeline/delivery/models.py
apps/code_review_pipeline/delivery/flow.py
apps/code_review_pipeline/delivery/feishu_approval.py
apps/code_review_pipeline/delivery/resume.py
app/cli.py
tests/unit/test_feishu_push_approval.py
tests/unit/test_delivery_flow.py
tests/unit/test_delivery_resume.py
tests/unit/test_delivery_cli.py
tests/unit/test_delivery_retry.py
plans/delivery-approval-timeout-20261123.md
```

## 4. 验收标准

- **T1 端口信号**：飞书审批等待到期 → 抛 `PushApprovalTimedOut`；
  pending HITL 被移除；不写 decision key；返回 `None` 的旧行为不再出现。
- **T2 流程映射**：flow 收到超时 → `state == ready_for_push_approval`、
  `failure.code == approval_timed_out`；publisher 未被调用；审计有对应记录。
- **T3 拒绝不变**：审批返回 `None` → 仍为 `REJECTED` +
  `approval_rejected`，publisher 未被调用。
- **T4 CLI 退出码**：超时记录 `delivery run` 退出码非 0，并打印
  `delivery resume --run-id`；既有 `waiting_ci` / 无 failure 的
  `ready_for_push_approval` 仍返回 0。
- **T5 可恢复**：对超时记录执行 `delivery resume`，批准后能进入 `PUSHING`
  并调用 publisher；再次超时则退出码非 0、状态不变、failure 仍为
  `approval_timed_out`、publisher 未被调用。
- **T6 不开新单**：`delivery retry` 对 `approval_timed_out` 拒绝，且不创建
  新 `DeliveryRun`。
- **T7 不回归**：既有审批/拒绝/恢复测试全绿；`state.py` 转移表未改。

## 5. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_feishu_push_approval.py tests/unit/test_delivery_flow.py tests/unit/test_delivery_resume.py tests/unit/test_delivery_cli.py tests/unit/test_delivery_retry.py tests/unit/test_delivery_stats.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

## 6. 禁止

- 把超时映射成 `REJECTED` / `approval_rejected`，或反过来把人工拒绝标成超时。
- 为超时新增状态或修改 `state.py` 的转移表（保持 `ready_for_push_approval` 可 resume）。
- 在超时路径调用 publisher / 创建分支 / 开 PR。
- 改动审批身份校验、bundle 绑定、`MergeExecutor` 语义。
- 新增依赖；关闭 lint / 放宽断言换通过。

## 7. 签收记录

（待所有者书面签收）
