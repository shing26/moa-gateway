# 多智能体实验台（Experiment Bench）

> 状态：首版与机器可读实验协议已实现。决策记录见
> [ADR-025](adr/adr-025-multi-agent-experiment-run-record.md) 与
> [ADR-026](adr/adr-026-experiment-protocol.md)。

## 目的

实验台回答的不是"界面上有没有多个 Agent"，而是：

1. 同一条协作链路是否真的执行了分工、依赖调度、工具调用、Critic 与治理；
2. 这次执行是否留下了足以复核的机器可读证据；
3. 同一任务、配置与工作区快照受控复跑时，可观察行为是否偏离。

首版不提供 UI、统计显著性检验或自动跨模型排名。验收面是**真实执行链路 + Run Record**。

## 数据模型

```text
Experiment
  └── Trial × N
        └── Run Record × 1
```

- **Experiment**：固定任务、适配器、工作区、验证命令、模型与运行配置。
- **Trial**：一次独立执行。每个 Trial 独立落盘，不覆盖旧记录。
- **Run Record**：一个 Trial 的可复核证据，不是普通日志或最终答案。

每个 Experiment 写到：

```text
.bench-experiments/<experiment-id>/
├── experiment.json
└── trials/
    ├── trial-000.json
    └── trial-001.json
```

`experiment.json` 是 manifest；`trial-*.json` 创建后拒绝覆盖。重复使用已有
`experiment-id` 会直接失败。

## 实验协议

首批实验由 [multi-agent-first-batch-v1.json](../bench/protocols/multi-agent-first-batch-v1.json)
冻结。协议是以下内容的唯一事实源：

- 8 个任务及其 E1/E3 来源；
- L0-L3 四个 arm 及版本；
- provider、模型、采样参数与单次输出上限；
- B1/B3 的预算轴、步数与 token 上限；
- pilot、baseline、formal 三阶段的重复次数；
- 基础设施错误的有限重试、不可重试结局与 pass-rate 排除规则；
- 只作方向性报告、不生成统计显著声明的边界。

`config_hash` 对协议身份做规范化 JSON 哈希。字段顺序不影响哈希；任务集、arm、模型、
预算、重复次数、重试、排除或报告口径的语义变化都会产生新哈希。协议单元映射到
Experiment 时，哈希进入配置身份，因此不会静默覆盖旧实验结果。

## CLI

权威批量入口是 `bench/run_batch.py`。默认 mock 模式不访问网络，只验证矩阵展开、
Experiment/Trial 落盘、完成跳过、中断恢复和报告：

```powershell
.\.venv\Scripts\python.exe bench/run_batch.py batch `
  --phase pilot `
  --tasks 0896629 60c64ed `
  --arms L0 L1 L2 L3 `
  --mode mock `
  --batch-id pilot-mock `
  --out .bench-experiments
```

批次产物：

```text
.bench-experiments/
├── <experiment-id>/
│   ├── experiment.json
│   └── trials/trial-*.json
└── _batches/<batch-id>/
    ├── batch.json
    ├── report.json
    └── report.md
```

`batch.json` 固定协议哈希与预期矩阵，记录每个单元的 `experiment_id`、完成状态、
Trial 数和 `resume_cursor`。重复执行同一 `batch-id` 时，已完成且有完整证据的单元
直接跳过；被中断的 Experiment 从缺失 Trial 继续；已有 Run Record 永不覆盖。

## 工作区写隔离

并行专家不直接写共享工作区。每个专家先获得源工作区的隔离副本，执行结束后只交回
文件级写集。批次图在下一派发节点按路径合并：

- 不同路径的写入全部保留，按路径和 `write_id` 确定性排序；
- 同一路径、同一内容的并行写入只落一次；
- 同一路径、不同内容的并行写入视为冲突，原始文件保持不变；
- 写集所基于的哈希与当前文件不一致时视为 stale-base 冲突；
- 冲突不静默选择最后写入者，而是把写入者、路径、前后哈希和候选写入留在
  Run Record 的 `workspace_conflicts` 证据中。

单一 Experiment 入口仍可直接使用：

离线 mock 闭环：

```powershell
$verify = @("python", "-m", "pytest", "-q") | ConvertTo-Json -Compress
.\.venv\Scripts\python.exe bench/run_experiment.py collab `
  --task "读取 README.md 并总结" `
  --trials 2 `
  --mode mock `
  --workspace . `
  --verify-command-json $verify `
  --out .bench-experiments/mock-check
```

本地真实模型闭环（要求 Ollama 已启动且已拉取模型）：

```powershell
$verify = @("python", "-c", "print('1 passed')") | ConvertTo-Json -Compress
.\.venv\Scripts\python.exe bench/run_experiment.py collab `
  --task "使用 coder 专家读取 pyproject.toml，并报告文件中的准确内容" `
  --trials 1 `
  --mode live `
  --provider local `
  --base-url http://localhost:11434/v1 `
  --model qwen2.5:3b `
  --workspace . `
  --verify-command-json $verify `
  --out .bench-experiments/live-check
```

bench L0-L3 适配器：

```powershell
.\.venv\Scripts\python.exe bench/run_experiment.py bench `
  --task-id 988fccb `
  --arm L1 `
  --trials 1 `
  --out .bench-experiments/bench-check
```

`bench/run_curve.py` 仅保留为旧 RunResult 曲线与 B3 规划的兼容分析入口，不再承担
协议矩阵的权威执行职责。

## Run Record

每条记录至少包含以下证据：

| 类别 | 字段 |
| --- | --- |
| 身份 | `schema_version`、`experiment_id`、`trial_id`、`trial_index` |
| 输入 | `task`、`task_hash`、`config_hash`、`model`、`config`、`verify_command` |
| 实际行为 | `plan`、`node_path`、`per_agent_outputs`、`final_text` |
| 提示词证据 | `prompt_hashes`，每次真实 LLM 请求的 messages + tool schemas 摘要 |
| 治理结果 | `evidence.critic`、`guard_action`、`error_code`、HITL 相关字段 |
| 资源 | `metrics`：调用数、token、成本、延迟、工具调用与错误 |
| 工作区 | `workspace.snapshot_id`、执行前后内容哈希与文件数 |
| 结论 | `status`、`duration_ms`、`error` |

`prompt_hashes` 不包含模型名、API key 或运行路径，因此同一请求上下文在不同模型间可比较。
模型与采样参数单独记录在 `model`。

## 受控复跑

```powershell
.\.venv\Scripts\python.exe bench/run_experiment.py collab `
  --task "同一个任务" `
  --mode mock `
  --workspace . `
  --out .bench-experiments/rerun `
  --experiment-id exp-rerun `
  --compare-to .bench-experiments/mock-check/<baseline-id>/experiment.json
```

比较关注：

- `status`、`task_hash`、`config_hash`；
- 模型与配置快照、验证命令；
- 实际 `plan`、`node_path`；
- Critic、Guard、HITL、错误码和工具调用计数；
- 工作区执行前内容哈希。

自然语言输出差异不自动算作偏离。`prompt_hashes` 会落盘供人工或后续分析器复核，
但由于 Critic prompt 可能包含上一轮模型输出，不默认做逐 Trial 严格相等比较。

Trial 数不一致会显式产生 `trial_count` divergence，不会静默截断后假装比较完成。

## 适配器边界

`CollaborationExperimentAdapter` 包装业务协作图，保留业务状态的治理证据；
`BenchExperimentAdapter` 包装既有 L0-L3 harness，保留 harness 的通过/失败判决。

两者只统一 Run Record 证据契约，**不合并执行引擎**。业务协作仍由
`CollabOrchestrator` 执行，bench 仍由既有 harness 执行；否则为了"统一"会制造一个
同时理解两套状态机、预算和失败语义的巨型调度器。

## 首版边界

- 每次执行串行；并发 Trial 不在首版。
- 本地 JSON 是唯一权威证据，稳定但无中心查询服务。
- 单 Trial 故障会记为 `status=error` 并继续后续 Trial；CLI 对存在 `error` 的实验返回非零。
- 不承诺逐 token 重放；多轮模型的 Critic prompt 本身可能随输出自然变化。
- 工作区指纹跳过 `.git`、`.venv`、缓存、`node_modules` 与 `.bench-work`。
