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
  --protocol <frozen-protocol-json> `
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

v5 正式 B1 完成后，B3 已按失败单元派生并完成 `96/96`、`32/32` 个完整单元，
0 error；宽松预算下仍是 96 个 `fail`。报告 SHA256 为
`3C87D4F34E5EADCCAF8BEA2E4F0F47707621311CA11139AE3911F9797B5C4AEA`。

`multi-agent-first-batch-v6-local.json` 固定 `qwen2.5:7b`，必须先用自己的 pilot
验证真实工具链。该模型在当前 4GB 显存上会部分回退 CPU；在 pilot 证明运行时间可接受
前，不得直接启动正式 `96` Trial 批次。

进入任何正式 B1 前同时满足：

1. pilot 在 4 小时内达到 `4/4`；
2. 四个 arm 都有真实模型调用和工具轨迹，且没有不可解释的 `error`；
3. pilot 通过 `verify_experiment_batch.py` 全量对账；
4. 基线 arm（协议 `statistics.baseline_arm`）至少有 3 条有效 Trial，且通过率
   不贴 0% 地板或 100% 天花板；落在 20%–80% 建议区间之外会输出功效 warning；
5. 用 pilot 的平均 Trial 耗时投影 96-Trial B1；投影超过 72 小时时先换模型或硬件，
   不用修改协议预算、删除 `error` 或降低重复次数来“跑完”。

区分度门禁与运行时间门禁由同一个脚本执行：

```powershell
.\.venv\Scripts\python.exe scripts/verify_pilot_runtime.py `
  --batch-id m9-local-qwen25-7b-pilot-live-r1 `
  --protocol bench/protocols/multi-agent-first-batch-v6-local.json `
  --out .bench-experiments
```

只有输出 `"ready": true` 才能启动正式 B1。脚本会同时检查 pilot 完成度、协议 arm
覆盖、真实模型调用、工具轨迹、`error`、基线区分度、4 小时 pilot 上限和
72 小时正式批次投影。

基线样本量与区间可通过参数调整（默认 3 条、0%/100% 硬线、20%–80% 建议
区间）：

```powershell
.\.venv\Scripts\python.exe scripts/verify_pilot_runtime.py `
  --batch-id <pilot-batch-id> `
  --protocol <protocol-json> `
  --min-baseline-trials 3 `
  --baseline-pass-rate-floor 0 `
  --baseline-pass-rate-ceiling 1 `
  --baseline-preferred-min 0.2 `
  --baseline-preferred-max 0.8 `
  --out .bench-experiments
```

两点边界：

- 20%–80% 是**经验起点，不是从当前数据推出的统计量**。Issue 分诊、CI 排障等
  新场景的任务难度分布与 PR 审查不同，上线时应先用各自 pilot 重新校准，
  避免把它当硬标准引用，也避免 warning 长期出现却被忽略。
- `baseline_arm` 目前**只能**由协议 `statistics.baseline_arm` 定义，CLI 不提供
  覆盖参数。这个边界是预注册原则的一部分：如果未来允许 CLI 覆盖，必须把
  覆盖值和来源（协议默认 vs 显式覆盖）写进门禁输出与 Run Record，保证
  事后可对账；不允许静默换基线绕过门禁。
- 门禁测试的默认姿势是**构造应被拒绝的边界输入并断言拒绝**；中间带数据
  只用来验证不误杀。禁止为了让测试通过而放宽实现边界（例如传入
  `-0.01/1.01` 去绕过 0%/100% 硬线）——这会让测试变成门禁的绕过工具。
- 软线 warning 目前不进入 CLI 退出码，这依赖“门禁是授权前人工检查步骤”
  这个前提。**任何自动化接入（CI、定时任务）必须先把 `warnings` 纳入
  退出码或独立告警通道，否则不得接入**，防止只看 exit code 的脚本静默
  吞掉功效不足信号。

当前 v6 pilot `m9-local-qwen25-7b-pilot-live-r1` 虽然完成 `4/4`、用时 1.237 小时、
投影 29.697 小时，但结果是 3 个 `fail`、1 个 `error`，门禁返回 `ready=false`：
L2 的两次 attempt 各重问 planner 三次，6 次输出都无法解析为计划，工具路径未启动。
报告 SHA256 为
`C24DBA7F07775E6F3F0A0FE4781101C80AD970F58F978E2A24883F1F43F577B8`。
该批次必须保留为失败证据，不能启动 v6 正式 B1。

`multi-agent-first-batch-v7-local.json` 固定同一模型与 `temperature=0.0`。v7 mock
用于验证协议和落盘；live pilot `m9-local-qwen25-7b-temp0-pilot-live-r1` 完成
`4/4` 和全量对账后仍为 2 个 `fail`、2 个 `error`，L0/L3 没有工具轨迹，
运行门禁返回 `ready=false`。该协议和批次已冻结，不能启动 v7 正式 B1。

v7 mock `m9-local-qwen25-7b-temp0-pilot-mock-r1` 已通过全量对账，报告 SHA256 为
`810D9A291118EF0F0CF21E555E92141B534DFA0726C623645C728DFE9E3F584E`。
v7 live pilot 报告 SHA256 为
`16C8FF3553FD59784CAA7FB5452358956815D2DAD4C0A88F80E2CD38FBCBB5A9`，
不得把 mock 的 `pass` 或诊断输出解释成能力证据。

`multi-agent-first-batch-v8-local.json` 使用 `qwen2.5:3b`、temperature 0.0 和
512-token 输出上限。v8 live pilot
`m9-local-qwen25-3b-temp0-512-pilot-live-r1` 完成 `4/4`、0 `error`，四个 arm
均有真实模型调用和工具轨迹；运行门禁返回 `ready=true`，用时 0.261 小时，
96-Trial 投影 6.258 小时。正式 B1 固定使用
`m9-local-qwen25-3b-temp0-512-formal-b1-live-r1`。

> [!IMPORTANT]
> 上段描述的是区分度门禁加入前的历史判定，v8 批次已冻结不重跑。按当前
> 门禁复验，该 pilot 的 L0 只有 1 条有效 Trial 且通过率为 0%，会返回
> `ready=false`（"基线 L0 有效样本 1 < 3"）。这正是 v8 正式批次四个 arm
> 全 0%、数据无区分力的源头；新协议必须先补足基线样本并让基线脱离地板。

复验 v8 pilot：

```powershell
.\.venv\Scripts\python.exe scripts/verify_experiment_batch.py `
  --batch-id m9-local-qwen25-3b-temp0-512-pilot-live-r1 `
  --protocol bench/protocols/multi-agent-first-batch-v8-local.json `
  --out .bench-experiments

.\.venv\Scripts\python.exe scripts/verify_pilot_runtime.py `
  --batch-id m9-local-qwen25-3b-temp0-512-pilot-live-r1 `
  --protocol bench/protocols/multi-agent-first-batch-v8-local.json `
  --out .bench-experiments
```

只有两条命令分别返回 `"ok": true` 和 `"ready": true`，才可启动或恢复 v8 B1。

`--max-concurrency 1` 的语义是全局最多一个 Trial 正在调用模型和执行工具，不保证
arm 按 `L0 → L1 → L2 → L3` 的启动顺序完成。各单元会先准备独立工作区，再竞争同一个
执行槽，因此 Manifest 中可能出现后续 arm 先运行、较早 arm 仍为 `pending` 的状态。
只要没有两个 Trial 同时越过执行槽，时间投影采用的串行并发契约仍成立。

本地大模型可能长时间占用 GPU 而没有 Python CPU 增长或日志更新。判断 runner 是否
仍然存活时，不能只看 Manifest 的 `updated_at`：

1. 确认 wrapper、runner 和 Python 子进程仍存在，并核对命令行指向当前批次；
2. 用 `netstat -ano` 确认 runner 与 `127.0.0.1:11434` 仍有 `ESTABLISHED` 连接；
3. 用 `ollama ps` 和 `nvidia-smi` 核对模型已加载且 GPU 正在工作；
4. 只有进程已经消失，或连接与 GPU 长时间均无活动且超过协议 `timeout`，才按暂停
   与恢复流程处理，不得因为观察窗口内没有新增 Trial 就重启。

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
  --batch-id <latest-resumed-batch-if-any> `
  --out .bench-experiments
```

只有输出 `"ready": true` 才能恢复 v4 B1；命令返回 `1` 表示尚未恢复，返回 `2`
表示输入批次或 Run Record 不可读取。检查器按 Batch Manifest 的 `updated_at`
排序，因此一个创建很早、后来才恢复的批次会排在其真实使用时间。任何无效 pilot
或 `429` 都会让此前的连续计数归零；恢复后再次暂停的批次也必须作为最后一条输入，
用来证明前一轮容量资格已经失效。

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
- `capacity-v4-l2-pilot-live-r2`：L2 `pass`，45 次调用、4 个子任务、0 次重试；
  与下一轮 L0 共同满足连续恢复门槛。
- `capacity-v4-l0-pilot-live-r6`：L0 `pass`，17 次调用、0 次重试；门禁返回
  `ready=true` 后恢复 v4 正式批次。
- `capacity-v4-l2-pilot-live-r3`：未出现 429，但 planner completion 被 4096
  上限截断，L2 最终为 `error`；45 次调用、399,261 token，不能作为恢复证据。
- `capacity-v4-l2-pilot-live-r4`：再次出现 429，Run Record 记录 6 次内部重试事件，
  最终为 `error`；39 次调用、268,232 token。两次新的恢复尝试都失败后，不再恢复
  v4 正式批次，改由独立 v6 协议验证其他模型。
- `m6-m9-formal-b1-v4-live-r1`：恢复后新增 `0896629__L0` 与
  `0896629__L1` 的 trial-001，均为 `pass` 且无 429；随后 L2 持续 429 约
  10 分钟没有新增 Run Record，runner 已停止，批次回到 `paused`：
  5 / 96 Trials、0 / 32 完整单元。把该批次作为最后一条门禁输入后必须重新返回
  `ready=false`，不能复用恢复前的 pilot 结论。

B3 只能从完整 B1 派生，并且只选择最终为 `fail` / `budget_exhausted` 的单元：

```powershell
.\.venv\Scripts\python.exe bench/run_batch.py b3 `
  --protocol <frozen-protocol-json> `
  --source-batch <completed-formal-b1-batch-id> `
  --phase formal `
  --mode live `
  --max-concurrency 1 `
  --rate-limit-per-minute 30 `
  --batch-id <new-formal-b3-batch-id> `
  --out .bench-experiments
```

v8 B3 固定使用：

```powershell
.\.venv\Scripts\python.exe scripts/verify_experiment_batch.py `
  --batch-id m9-local-qwen25-3b-temp0-512-formal-b1-live-r1 `
  --protocol bench/protocols/multi-agent-first-batch-v8-local.json `
  --out .bench-experiments

.\.venv\Scripts\python.exe bench/run_batch.py b3 `
  --protocol bench/protocols/multi-agent-first-batch-v8-local.json `
  --source-batch m9-local-qwen25-3b-temp0-512-formal-b1-live-r1 `
  --phase formal `
  --mode live `
  --max-concurrency 1 `
  --rate-limit-per-minute 30 `
  --batch-id m9-local-qwen25-3b-temp0-512-formal-b3-live-r1 `
  --out .bench-experiments
```

跨模型比较只接受已完成、矩阵一致且各自只含一个模型的独立批次：

```powershell
.\.venv\Scripts\python.exe bench/run_batch.py compare `
  --protocol bench/protocols/multi-agent-first-batch-v8-local.json `
  --batch-id m9-local-qwen25-3b-formal-b1-live-r1 `
  --batch-id m9-local-qwen25-3b-temp0-512-formal-b1-live-r1 `
  --out .bench-experiments
```

产物写入 `.bench-experiments/_comparisons/<comparison-id>/`。报告并列显示
`model × arm` 的描述性指标，并分别运行每个模型的预注册配对分析；
`cross_model_inferential_claim` 固定为 `false`，不能把不同模型合并后宣称显著性。

v8 B3 与 v5 × v8 比较的实测结果：

- v8 B1：`96/96`、`32/32`、0 缺口，0 `error`，报告 SHA256
  `dc534e0a6182d4293b16dad4c36d15f219efd4db7ca5c8ba76591b29811d4955`；
- v8 B3：`96/96`、`32/32`、0 缺口，76 `fail`、17 `error`，报告 SHA256
  `be9d8c4489252fc2ac681bf280bf91ac66a74afced037fec923a68188afbaefd`；
  17 个 `error` 中 14 个是单次 completion 512 上限截断、3 个是 Critic 未产出
  终局，均发生在 L1/L2/L3；这是如实记录，不允许改写成 `fail`；
- v5 × v8 比较：`cross-model-20261024T123015Z-c5d7fb84`。两个批次按
  “模型名 + temperature + max_tokens” 的配置身份分别分析；v5 授权分析并得到
  `no_significant_difference`，v8 B1 因部分 L3 单元有效样本不足保持 `blocked`。
  同名模型的不同协议配置不会被合并。

  **勘误（2026-10-25）**：上述旧工件的 `analysis_authorized_by_model` /
  `statistical_claims_by_model` 字段曾以裸模型名为键，同名模型不同配置时
  后者覆盖前者——本例中 v8 的 `false` 被 v5 的 `true` 覆盖，v8 的未授权
  状态在旧工件 JSON 里被隐藏。该缺陷不影响本节叙述（叙述依据各批次自身
  `report.json`，v5=`true`、v8=`false` 口径本就正确），但旧字段不可再作为
  授权依据引用。修复后的字段为 `analysis_authorized_by_batch` /
  `statistical_claims_by_batch`（按批次 ID 键控）；修复后重建工件为
  `cross-model-20261024T195243Z-c50fe00e`（comparison.json SHA256
  `9b7f91da27f40239ab133930b6fdbd2026558d9f7cff4e27e474891ac864b26e`），
  v8=`false`（L3 有效样本不足）与 v5=`true`（`no_significant_difference`，
  p=1.0）逐条复核成立。

### 工件哈希记录规范

文档中嵌入的工件 SHA256 必须由脚本输出后复制，**禁止手抄或凭记忆转写**：
批次报告哈希取自 `scripts/verify_experiment_batch.py` 输出的
`report_sha256` 字段；comparison 工件哈希用
`Get-FileHash -Algorithm SHA256` 计算后从命令输出复制。提交前对文档中
每个哈希与磁盘工件做一次自动比对。哈希是机器可靠生成、人工不可靠复制
的值——2026-10-25 勘误中一处 SHA256 曾在手抄时错一个字符，靠复核抓住；
本条规则把这类纠错从运气变成机制。

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
- `multi-agent-first-batch-v6-local.json` 固定本机 `qwen2.5:7b`，用于 v4 端点
  持续失败后的独立验证；它不是 v4 的替代样本，也不能与 v4 合并成同一个模型结论。

## 7. 发布验收


1. 新环境按本手册启动服务；
2. `/dashboard/api/experiments` 返回至少一个已完成批次；
3. 批次详情中的数字与持久化 `report.json` 一致；
4. 至少打开一条 Run Record，确认路径、模型、steps、metrics 和 workspace 可见；
5. 记录部署 URL、批次 ID、报告哈希和检查时间。

本地 Compose 的参考入口是 `http://127.0.0.1:18081/dashboard/experiments`。共享或生产
环境必须把同一镜像和只读实验卷部署到受控主机，并记录实际外部 URL。

## 8. Issue 分诊场景

Issue 分诊复用同一条 pilot 门禁轨道，任务定义在
`bench/triage/tasks/*.triage.json`，协议为
`bench/protocols/issue-triage-v1-local.json`。判定口径：**category + route
全对为 pass，priority 仅记录不判决**——优先级标注带强主观性，把它放进
判决会让任务退化为"复现标注者直觉"，r1 轮四 arm 全 0% 正是栽在这。

三轮 pilot 的实测演进（批次均已冻结，不修改）：

- `issue-triage-v1-pilot-live-r1`：分诊 workspace 为空目录，只读工具全部
  空转（"扫描 0 个文件"），四 arm 0/32。区分度门禁以"基线 L0 通过率
  0% 地板"拦截。修复：workspace 物化为仓库 HEAD 快照，与代码任务同构。
- `issue-triage-v1-pilot-live-r2`：三字段全对口径下仍 0/32，但逐字段
  分解显示 category 7/8、route 5/8、priority 仅 1/8——失败集中在
  priority。据此把判定改为 category + route，priority 降为记录字段。
- `issue-triage-v2-pilot-live-r1`：基线 L0 = 3/8（37.5%），脱离地板、
  落入 20%–80% 建议区间；门禁这次拦截在 **t1 L2 无工具轨迹**（planner
  在 512 token 上限下 11 次 LLM 调用未产出可执行计划，0 次工具调用）。
  L1/L2/L3 均 0/8 且全部"未找到可解析分诊 JSON"。

当前结论：**该协议不授权正式批次**。处置方向按优先级：

1. 提高模型档位或放宽单次 completion 上限，使 L2 planner 能产出计划
   （t1 L2 的 11 次调用 0 工具是结构性失败，不是随机噪声）；
2. 若维持 qwen2.5:3b + 512，则 Issue 分诊场景只保留 L0 通路，多智能体
   档位不进入正式对比——并在协议中显式声明该边界，而不是让它空转烧
   token；
3. 换更强模型重跑 pilot 前不改任务集——基线已在带内，任务难度校准完成。

预算口径：v1 协议 B1 = 12 步 / 60k token、B3 = 36 步 / 180k token。
v2 轮实测 L0 ≈ 2.1k token/任务，L1 ≈ 13.6k，L2 ≈ 23.8k，L3 ≈ 13.6k，
多档为 L0 的 6–11 倍，与 PR 审查场景的 7.9–14.8 倍同量级。
