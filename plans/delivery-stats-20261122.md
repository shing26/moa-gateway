# 契约：`delivery stats`（交付产出度量）

- **task id**：`delivery-stats-20261122`
- **分支**：`feat/delivery-stats-20261122`
- **背景**：review 复盘结论——改进（重复读抑制、失败续跑）不可见，导致"原地踏步"的
  主观感受。需要一个从 `data/delivery-runs/*.json` 直接算产出的命令：成功率、
  token、时长、失败码分布、续跑率。
- **所有者签收**：用户消息「按顺序执行」（2026-11-22，自用主线第 2 步）。

## 1. 语义

`python -m app.cli delivery stats [--json] [--since YYYY-MM-DD]`
只读扫描 `settings.delivery_run_record_dir` 下的 `*.json`，输出：

- `runs`：记录总数（含解析失败计数 `unreadable`，不得静默丢弃）
- `by_state`：每个 `state` 的条数
- `delivered`：`state == merged` 的条数
- `failed`：`state ∈ {failed, budget_exhausted, rejected, needs_human}`
- `in_flight`：其余状态
- `completion_rate`：`delivered / (delivered + failed)`，分母为 0 时为 `null`
- `tokens`：`usage.prompt_tokens/completion_tokens/total_tokens/cost_usd` 的**总和**
  与**已终态单平均值**（delivered+failed 为分母）
- `duration`：`updated_at - started_at` 的 p50 / p90（秒，已终态单；不足 2 条时
  p50=p90=该值或 `null`）
- `failure_codes`：`failure.code` 分布（无 failure 的记 `null`，不丢）
- `retries`：`request.source_ref` 以 `retry:` 开头的条数，及其 `merged` 比例
- `--since` 按 `started_at` 日期过滤（含当天，按本地日期比较）
- `--json` 输出稳定键；人类可读输出是对齐的纯文本表，不引入依赖

## 2. 允许路径（Allowed paths）

```
apps/code_review_pipeline/delivery/stats.py   （新建，纯函数 + 聚合）
app/cli.py                                     （新增 cmd_delivery_stats + parser 注册）
tests/unit/test_delivery_stats.py              （新建）
plans/delivery-stats-20261122.md
```

## 3. 验收标准

- **T1 聚合正确**：夹具目录含 merged / failed(VERIFY_FAILED) /
  budget_exhausted(INFRA_ERROR) / rejected(APPROVAL_REJECTED) / waiting_ci 各一条 →
  `runs=5`、`delivered=1`、`failed=3`、`in_flight=1`、`completion_rate=0.25`；
  token/时长按契约口径计算。
- **T2 坏记录不崩**：一个非法 JSON 文件 → `unreadable=1`、其余照常聚合、退出码 0。
- **T3 空目录**：`runs=0`、`completion_rate=null`、人类可读输出不炸。
- **T4 `--since`**：早于阈值的记录被排除；阈值当天包含。
- **T5 `--json`**：键稳定、值为 JSON 可序列化；`null` 用 JSON null。
- **T6 续跑统计**：`source_ref="retry:..."` 的单计数正确。
- **T7 只读**：不写任何文件、不联网、不改 run store / models。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit/test_delivery_stats.py -q
V2  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m ruff check .
```

外加冒烟：对真实 `data/delivery-runs` 跑一次 `delivery stats` 与 `--json`，贴输出
（只读，允许慢）。

## 5. 禁止

- 新增第三方依赖；读写数据库；改 `models.py` / `run_store.py`。
- 把解析失败静默跳过（必须计入 `unreadable`）。
- 把 `null` 比率写成 `0`（0% 与"没有数据"必须可区分）。
- 触碰 Allowed paths 之外的模块。

## 6. 签收记录（2026-11-23）

**所有者书面签收**：用户消息「签收 #126 #127 #128 #129」（2026-11-23）。

**合并**：PR #126 → `main`，merge commit
`d82a3f0128d6ca651bbd9335e89991e2faa56ce1`。

**验收证据**：独立 Verifier `ACCEPT_WITH_NITS`。V1 `8 passed`；V2
`2289 passed, 6 skipped`；V3 `All checks passed!`；真实 192 条记录冒烟
`runs=192 delivered=9 failed=122 in_flight=61 completion=6.87%`，交叉校验一致。
GitHub CI `test` / `integration` 绿，`docker` skipped。

**残余（非阻塞，如实记录）**：T4 单测只覆盖 naive 时间字符串；`tokens` 总和含
in-flight、均值分母为终态数（契约字面口径）；p50/p90 为 nearest-rank。
