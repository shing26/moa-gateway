# 契约：实现器迭代用「快验证车道」

- **task id**：`delivery-fast-verify-lane-20261122`
- **分支**：`feat/fast-verify-lane-20261122`
- **背景**：`ProductionImplementer` 的 `run_tests` 把 selector 追加到
  `verify_command` 末尾，而本仓档案的 `verify_command` 已经带 `tests/unit`
  （`config/repos/shing26/moa-gateway.toml`），因此模型每"跑一下测试"都要
  跑全量单测（实测 7–12 分钟）。产出周第 1 单在 2.5 分钟内烧掉 41 次
  `read_file` 调用、38.7 万 token，主要时间花在等待全量测试。
  确定性门禁必须保持全量；实现器的**迭代**只需要窄范围反馈。

## 1. 语义

1. 仓库档案新增可选字段 `fast_verify_command`（字符串，同一套 argv 解析）。
2. 未配置时，快车道回退为 `verify_command`（行为不变）。
3. `ResolvedDeliveryConfig.fast_verify_command` 恒有值；另给
   `fast_verify_source`（`profile <path>` / `derived from verify_command`）。
4. `delivery run` 与失败续跑把 `fast_verify_command` 传给
   `ProductionImplementer`；`ChangePipeline` 的确定性门禁仍用
   `verify_command`（全量），两者不得混用。
5. 坏 `fast_verify_command`（非字符串/空串拆分后为空）按档案损坏
   fail-closed（`RepoProfileError`），不回退环境变量。

## 2. 允许路径（Allowed paths）

```
apps/code_review_pipeline/delivery/repo_profile.py
app/cli.py
apps/code_review_pipeline/delivery/retry.py
config/repos/shing26/moa-gateway.toml
tests/unit/test_repo_profile.py
tests/unit/test_delivery_retry.py
tests/unit/test_delivery_cli.py
plans/delivery-fast-verify-lane-20261122.md
```

## 3. 验收标准

- **T1 解析**：档案含 `fast_verify_command` → 解析为 tuple；缺失 → 等于
  `verify_command`；空串 → `RepoProfileError`。
- **T2 隔离**：`delivery run` 构造的 `ProductionImplementer` 收到快车道命令；
  `DeliveryPolicy.verify_command` 与 `ChangePipeline` 仍是全量命令。
- **T3 续跑**：`retry.py` 的真实执行栈同样使用快车道，门禁仍全量。
- **T4 提示**：`delivery run` 启动日志打印快车道来源，便于核对。
- **T5 不回归**：无 `fast_verify_command` 的仓库/环境变量路径行为不变。
- **T6 只改允许路径**，不新增依赖。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_repo_profile.py tests/unit/test_delivery_retry.py tests/unit/test_delivery_cli.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

## 5. 禁止

- 让门禁使用快车道命令；或让快车道影响 `DeliveryPolicy`。
- 在 `fast_verify_command` 损坏时静默回退。
- 触碰 Allowed paths 之外的模块。

## 6. 签收记录（2026-11-23）

**所有者书面签收**：用户消息「签收 #126 #127 #128 #129」（2026-11-23）。

**合并**：PR #128 → `main`，merge commit
`f8f215e2b49fae42a63bf8574730b7e8f8778a58`。

**验收证据**：独立 Verifier `ACCEPT_WITH_NITS`。V1 `88 passed`；V2
`2285 passed, 6 skipped`；V3 `All checks passed!`；T1–T6 全 PASS；
`DeliveryPolicy` 仍收全量命令，只有 `ProductionImplementer` 用快车道。
GitHub CI 绿。

**残余（非阻塞）**：空 selector 时 `python -m pytest -q` 会收集 `tests/` 全部
用例（已由 PR #129 的 selector 必填守卫闭合）；T2/T3 接线当时只有静态行号证据。
