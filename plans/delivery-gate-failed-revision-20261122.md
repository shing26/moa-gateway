# 契约：门禁失败（GATE_FAILED）进入修订回路

- **task id**：`delivery-gate-failed-revision-20261122`
- **分支**：`fix/gate-failed-revision-20261122`
- **背景**：产出周第 1 单（`dlv-8c25c14f…`）被强制 lint 门禁拦下
  （`tests/unit/test_delivery_retry.py:F401` 未使用 import），但
  `PreparationStatus.INVALID` 把它归成 `needs_human + infra_error`：
  既不把门禁结论回流给实现方修订，也不在 `delivery resume` 的可续跑面内。
  门禁失败本质是"改代码就能过"的确定性失败，应与 `VERIFY_FAILED` 同类。
- **所有者**：用户「按顺序执行」（2026-11-22 自用主线）。

## 1. 语义

1. `PreparationStatus` 新增 `GATE_FAILED = "gate_failed"`。
2. `GitHubChangeAdapter.prepare` 把 `ChangeStatus.GATE_FAILED` 映射为
   `PreparationStatus.GATE_FAILED`（携带 bundle 与 message，`verification=None`）。
3. `DeliveryFlow` 的修订集合加入 `GATE_FAILED`：未超修订次数时进入
   `REVISING`，feedback 必须包含门禁原文（`preparation.message`）。
4. 修订次数耗尽后：`failure.code = VERIFY_FAILED`、状态仍为
   `NEEDS_HUMAN`（与既有 `VERIFY_FAILED` 终态一致，不改变既有断言）。
5. 其余 `INVALID` 仍为 `infra_error + needs_human`。

## 2. 允许路径（Allowed paths）

```
apps/code_review_pipeline/delivery/ports.py
apps/code_review_pipeline/delivery/adapters/github.py
apps/code_review_pipeline/delivery/flow.py
tests/unit/test_delivery_flow.py
tests/unit/test_delivery_github_adapter.py
plans/delivery-gate-failed-revision-20261122.md
```

## 3. 验收标准

- **T1 映射**：`ChangeStatus.GATE_FAILED` → `PreparationStatus.GATE_FAILED`，
  bundle 保留、message 保留、verification 为 `None`。
- **T2 修订**：首次 `GATE_FAILED` 触发一轮修订，feedback 含门禁原文；
  第二次 `READY` 后流程继续到 `READY_FOR_PUSH_APPROVAL`。
- **T3 耗尽**：连续 `GATE_FAILED` 用尽修订次数 → `NEEDS_HUMAN` +
  `FailureCode.VERIFY_FAILED`，不调用 publisher，不多跑实现轮。
- **T4 不回归**：既有 `VERIFY_FAILED` / `NO_CHANGES` / `INVALID` 行为不变。
- **T5 只改允许路径**，不新增依赖。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_delivery_flow.py tests/unit/test_delivery_github_adapter.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

## 5. 禁止

- 修改 `RESUMABLE_FAILURE_CODES` / `RETRY_STATES`（本单不改续跑白名单）。
- 弱化或删除既有断言；改变 `VERIFY_FAILED` 的终态。
- 触碰 Allowed paths 之外的模块。
