# 契约：`run_tests` 的 selector 必填守卫

- **task id**：`run-tests-selector-20261123`
- **分支**：`feat/run-tests-selector-20261123`
- **背景**：#128 给实现器接了快车道（`python -m pytest -q`）。Verifier 指出：
  selector 为空时 `pytest` 按 `testpaths=["tests"]` 收集全部 2345 条用例，
  比全量 `tests/unit`（2291）还宽，且会跑 7–12 分钟。实测 40 步预算里
  两次全量 `run_tests` 就耗掉 14 分钟。实现器的迭代验证必须显式指定范围。
- **所有者**：用户「加 selector 必填守卫」（2026-11-23）。

## 1. 语义

1. `build_toolset(...)` 新增 `require_test_selector: bool = False`；递归
   `bind_workspace` 必须原样转发。
2. 为 True 时，`run_tests` 的 `selector` 在工具描述里标注"必填"，且
   **空串/纯空白**直接拒绝执行，observation 指出该传什么（文件路径或
   `::测试名`），并说明全量门禁由流水线单独执行。命令**不得**被执行。
3. 为 False 时行为完全不变（兼容 bench 与既有调用方）。
4. `ProductionImplementer` 构造工具集时传 `require_test_selector=True`，
   并在系统提示中写明 `run_tests` 必须带 selector。

## 2. 允许路径（Allowed paths）

```
app/agents/workspace_tools.py
apps/code_review_pipeline/delivery/adapters/production_agent.py
tests/unit/test_bench_tools.py
tests/unit/test_delivery_production_agent.py
plans/run-tests-selector-20261123.md
```

## 3. 验收标准

- **U1 守卫生效**：`require_test_selector=True` + `selector=""`（或纯空白）→
  调用被拒，observation 含 `selector` 与示例（`tests/` 或 `::`），且验证命令
  **没有**产生任何副作用（探针文件不存在）。
- **U2 正常放行**：同一工具集带非空 selector → 命令执行，observation 含退出码。
- **U3 默认不变**：`require_test_selector` 未传 → 空 selector 仍执行基底命令。
- **U4 接线**：`ProductionImplementer.run` 构造工具集时
  `require_test_selector=True`（用 spy 捕获）；系统提示包含"必须带 selector"。
- **U5 工具描述**：守卫开启时 `selector` 的 description 明确写必填。
- **U6 只改允许路径**，不新增依赖，不改 `parse_selector` 的安全边界。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_bench_tools.py tests/unit/test_delivery_production_agent.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

## 5. 禁止

- 通过 schema 静默把空 selector 变成别的默认路径（必须明确拒绝）。
- 影响 bench / 非交付调用方的既有行为。
- 触碰 Allowed paths 之外的模块。

## 6. 签收记录（2026-11-23）

**所有者书面签收**：用户消息「签收 #126 #127 #128 #129」（2026-11-23）。

**合并**：PR #129 → `main`，merge commit
`1b2b71fc71f3dc07c022db3d1b2fdc95fe2ca8e3`。

**验收证据**：独立 Verifier `ACCEPT`。V1 `76 passed`；V2
`2287 passed, 6 skipped`；V3 `All checks passed!`；U1–U6 全 PASS；
空/空白 selector 在创建验证副本与执行命令之前被拒绝（探针文件不存在）。
GitHub CI 绿。

**残余（非阻塞）**：系统提示的必填语义只有源码证据，暂无自动化断言。
