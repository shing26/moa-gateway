# 多智能体实验台运行手册

> 适用版本：Run Record v1、Batch Manifest v1-v3、协议 v3
> 目标：让 mock 验证、live 实验、只读查看和故障恢复都有一条可执行路径

## 1. 环境与边界

- Python 使用仓库 `.venv`，解释器为 3.12。
- 实验产物默认写入 `.bench-experiments/`，该目录不进 Git。
- CI 只运行 mock 和离线测试。`bench/run_batch.py` 检测到 `CI=true` 时会拒绝
  `--mode live`，除非显式设置 `BENCH_ALLOW_LIVE_IN_CI=1`。
- live 批次必须由人手动启动，不能挂进普通 CI。
- 当前 NVIDIA 端点没有美元成本信号。成本显示为不可用时，以 token 为等预算主轴，
  不能把 `$0` 解释成零成本。

## 2. 启动服务

```powershell
$env:BENCH_EXPERIMENTS_DIR = (Resolve-Path .bench-experiments).Path
$env:GATEWAY_ALLOW_INSECURE = "1"   # 仅本地只读验收
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8099
```

生产或共享环境必须配置 `DASHBOARD_PASSWORD` 或 `WEBHOOK_AUTH_TOKEN`，不应开启
`GATEWAY_ALLOW_INSECURE`。

只读入口：

- `/dashboard/experiments`：批次列表；
- `/dashboard/experiments/<batch-id>`：样本量、成本、错误和 Run 链接；
- `/dashboard/api/experiments`：只读批次 JSON；
- `/dashboard/experiments/<batch-id>/runs/<experiment-id>/<trial-index>`：
  完整 Run Record。

验收要求：页面里的完成数、缺失数、样本量、通过率、token 和成本必须与
`_batches/<batch-id>/report.json` 一致。

## 3. Mock 验收

```powershell
$env:CI = "true"
.\.venv\Scripts\python.exe bench/run_batch.py batch `
  --phase pilot `
  --tasks 0896629 `
  --arms L0 L3 `
  --mode mock `
  --batch-id ci-mock-check `
  --out .bench-experiments
```

Mock 结果只能证明矩阵、落盘、恢复、报告和 UI 契约，不能作为模型能力证据。

## 4. Live 批次

正式 B1：

```powershell
$env:CI = ""
.\.venv\Scripts\python.exe bench/run_batch.py batch `
  --phase formal `
  --mode live `
  --max-concurrency 1 `
  --rate-limit-per-minute 30 `
  --batch-id m6-formal-b1-live-r2 `
  --out .bench-experiments
```

并发和启动速率会写入 Batch Manifest；恢复时参数漂移会被拒绝。端点容量未知时先
从并发 1 开始。出现持续 429 时应停止扩并发，不能把限流错误算成 arm 失败。

B3 只能从完整 B1 派生，并且只选择最终为 `fail` / `budget_exhausted` 的单元：

```powershell
.\.venv\Scripts\python.exe bench/run_batch.py b3 `
  --source-batch m6-formal-b1-live-r2 `
  --phase formal `
  --mode live `
  --batch-id m6-formal-b3-live-r2 `
  --out .bench-experiments
```

## 5. 错误与恢复

- `error` 按协议最多重试一次；每个 attempt 的 usage、错误和工作区快照保存在
  `evidence.retry_attempts`。
- `fail` 和 `budget_exhausted` 是实验结局，不重试。
- 取消或进程退出后，先用 `batch.json` 和实际进程确认是否仍有 runner 持有批次。
  只有确认进程已不存在时，才可标记为暂停：

```powershell
.\.venv\Scripts\python.exe scripts/pause_experiment_batch.py `
  --batch-id <batch-id> `
  --out .bench-experiments
```

- 同一批次的并发、速率和协议哈希不可漂移。端点容量需要改变时，结束旧批次并创建
  新批次 ID，保留旧批次的 error/paused 证据。
- 已有 Run Record 永不覆盖。恢复只补缺失的连续 Trial。

## 6. Schema 与依赖

- 当前 reader 支持 Batch Manifest v1-v3；v1 缺失的并发字段按 `1` 和 `0` 读取。
- Run Record 当前为 v1。
- CI 先执行 `uv lock --check`，再运行迁移契约测试；未识别的 schema 版本必须失败，
  不得静默按最新版解释。
- 修改协议任务、arm、模型、预算、重复、重试、排除或报告口径时，必须产生新协议
  文件和新的 `protocol_hash`。

## 7. 发布验收

1. 新环境按本手册启动服务；
2. `/dashboard/api/experiments` 返回至少一个已完成批次；
3. 批次详情中的数字与持久化 `report.json` 一致；
4. 至少打开一条 Run Record，确认路径、模型、steps、metrics 和 workspace 可见；
5. 记录部署 URL、批次 ID、报告哈希和检查时间。
