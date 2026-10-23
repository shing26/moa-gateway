# 多智能体实验台运行手册

> 适用版本：Run Record v1、Batch Manifest v1-v3、协议 v4 / v5-local
> 目标：让 mock 验证、live 实验、只读查看和故障恢复都有一条可执行路径

## 1. 环境与边界

- Python 使用仓库 `.venv`，要求 3.12+；当前验证解释器为 3.13。
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

容器化只读入口：

```powershell
$env:GATEWAY_PORT = "18081"
$env:DASHBOARD_PASSWORD = "<set-a-local-password>"
docker compose -f docker-compose.dev.yml up -d --build gateway
```

实际宿主机端口以 `.env` 的 `GATEWAY_PORT` 或 `docker compose ... ps` 为准；上面的
命令用 shell 环境变量覆盖为 `18081`。当前工作区部署使用 `8083`。

Compose 将仓库的 `.bench-experiments` 以只读方式挂载到容器的
`/app/.bench-experiments`，并设置 `BENCH_EXPERIMENTS_DIR`。实验产物不会被打进镜像，
容器也没有写实验目录的权限。

验收要求：页面里的完成数、缺失数、样本量、通过率、token 和成本必须与
`_batches/<batch-id>/report.json` 一致。

报告用两个字段区分分析资格与结论强度：`analysis_authorized=true` 表示预注册分析
可以执行；`statistical_claims=true` 只在明确得到 `candidate_better` 或
`candidate_worse` 时出现。`no_significant_difference` 不是“arm 无差异”的证明，
也不得被页面或文档改写为显著优势。

完整批次可用确定性脚本对账：

```powershell
.\.venv\Scripts\python.exe scripts/verify_experiment_batch.py `
  --batch-id <completed-batch-id> `
  --protocol bench/protocols/<protocol>.json `
  --out .bench-experiments
```

脚本会重建报告、核对全部 Run Record、来源路径、Trial 序号以及只读 API 服务返回；
任一不一致都会返回非零状态。

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
  --batch-id <new-formal-b1-batch-id> `
  --out .bench-experiments
```

并发和启动速率会写入 Batch Manifest；恢复时参数漂移会被拒绝。端点容量未知时先
从并发 1 开始。出现持续 429 时应停止扩并发，不能把限流错误算成 arm 失败。

本地第二模型先跑独立 pilot，再进入自己的正式批次：

```powershell
.\.venv\Scripts\python.exe bench/run_batch.py batch `
  --protocol bench/protocols/multi-agent-first-batch-v5-local.json `
  --phase pilot `
  --tasks 0896629 `
  --arms L0 L1 L2 L3 `
  --mode live `
  --max-concurrency 1 `
  --rate-limit-per-minute 30 `
  --batch-id <new-local-pilot-batch-id> `
  --out .bench-experiments
```

该协议固定 `qwen2.5:3b` 与 `http://127.0.0.1:11434/v1`。pilot 只验证真实工具链，
不进入能力比较；正式批次必须使用自己的批次 ID，且不能与 NVIDIA 批次混写。

容量阻塞的判定与处理：

1. 记录批次启动时间、最后一条 Run Record 时间、Trial 数量和连续 429 时长；
2. 若长时间没有新增 Run Record 且日志持续为 429，停止 runner 并确认进程已退出；
3. 用 `pause_experiment_batch.py` 把孤立批次标为 `paused`；
4. 该批次只作为基础设施证据，不进入 arm 比较，也不得被 UI 显示成“正在运行”；
5. 端点恢复后创建新批次 ID。只有原协议、模型、并发和速率均不漂移且存在可恢复
   连续缺口时，才考虑恢复原批次。

恢复 v4 正式批次前，至少连续完成两个真实 agent pilot，其中至少一个使用 L2。
任何一次出现 `429` 都重新计算门槛；8/512-token 探测或单个 L0 成功不构成容量恢复。

Run Record 会保留被内部重试吸收的 `llm_retry_events`。旧记录如果没有该字段，
检查器不会把它算作有效恢复证据。门禁本身可执行：

```powershell
.\.venv\Scripts\python.exe scripts/verify_capacity_gate.py `
  --protocol bench/protocols/multi-agent-first-batch-v4.json `
  --batch-id <pilot-1> `
  --batch-id <pilot-2> `
  --out .bench-experiments
```

只有输出 `"ready": true` 才能恢复 v4 B1；命令返回 `1` 表示尚未恢复，返回 `2`
表示输入批次或 Run Record 不可读取。检查器按批次创建时间排序，任何无效 pilot
或 `429` 都会让此前的连续计数归零。

已记录的阻塞批次：

- `m6-formal-b1-live-r1`：并发 4 触发持续 429，0 条 Trial，作废；
- `m6-formal-b1-live-r2`：并发 1 仍只完成 3 条 Trial、0 个完整单元，暂停保留。
- `capacity-v4-l0-pilot-live-r3`：v4 L0 单任务真实 pilot 连续 429，最终 1 个
  `error`、0 个有效样本；只作容量证据，不进入正式比较。
- `capacity-v4-l0-pilot-live-r4`：首个真实调用 429，内部退避后 L0 通过；因出现过
  429，本次不计入连续恢复门槛。
- `capacity-v4-l2-pilot-live-r1`：L2 首个 attempt 出现 429，第二次 attempt 的
  planner 又被 4096 completion 上限截断，最终 1 个 `error`、0 个有效样本。
- `capacity-v4-l0-pilot-live-r5`：L0 得到有效 `fail`，但 Run Record 捕获到
  2 次 429；`verify_capacity_gate.py` 返回 `ready=false`，证明重试遥测与门禁
  已能在不依赖终端日志的情况下自行判断容量未恢复。

B3 只能从完整 B1 派生，并且只选择最终为 `fail` / `budget_exhausted` 的单元：

```powershell
.\.venv\Scripts\python.exe bench/run_batch.py b3 `
  --source-batch <completed-formal-b1-batch-id> `
  --phase formal `
  --mode live `
  --batch-id <new-formal-b3-batch-id> `
  --out .bench-experiments
```

跨模型比较只接受已完成、矩阵一致且各自只含一个模型的独立批次：

```powershell
.\.venv\Scripts\python.exe bench/run_batch.py compare `
  --protocol bench/protocols/multi-agent-first-batch-v4.json `
  --batch-id <completed-model-a-batch> `
  --batch-id <completed-model-b-batch> `
  --out .bench-experiments
```

产物写入 `.bench-experiments/_comparisons/<comparison-id>/`。报告并列显示
`model × arm` 的描述性指标，并分别运行每个模型的预注册配对分析；
`cross_model_inferential_claim` 固定为 `false`，不能把不同模型合并后宣称显著性。

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

本地 Compose 的参考入口是 `http://127.0.0.1:18081/dashboard/experiments`。共享或生产
环境必须把同一镜像和只读实验卷部署到受控主机，并记录实际外部 URL。
