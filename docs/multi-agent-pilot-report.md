# 多智能体工程试点报告（M4）

> 状态：已完成
> 报告日期：2026-10-23
> 批次：`m4-pilot-live-r4`
> 协议：`multi-agent-first-batch` v3
> 协议哈希：`20b236344df8bf44f7e8417be6a029c65d1805bc4e608cb91c76eeed6dad8701`
> 上位决策：[ADR-025](adr/adr-025-multi-agent-experiment-run-record.md)、
> [ADR-026](adr/adr-026-experiment-protocol.md)、
> [ADR-027](adr/adr-027-workspace-write-isolation.md)

## 结论边界

本轮验收目标是证明实验台能够真实执行并留下可复核记录，不是证明某个 arm 更好。
矩阵为 `2 任务 × 4 arm × 1 次 = 8 runs`，每个非空格只有一个样本；任何通过率差异
都只能作为后续实验的假设，不能据此宣称统计优势。

`m4-pilot-live-r3` 已被本批次取代。r3 虽然完成 8 个单元，但 L2/L3 的 Run Record
没有实际计划，且轨迹只剩 1 条汇总步骤，不满足 M4 的可追溯性要求。修复计划与步骤
记录后，r4 使用新的 Experiment ID 完整重跑，没有覆盖或复用 r3 证据。

## 固定条件

- Provider：`nvidia`
- 模型：`nvidia/nemotron-3.5-lightning-30b-a3b`
- 采样：`temperature=1.0`
- 单次输出上限：`4096` token
- 预算档：`B1`
- 任务来源：E1（真实历史缺陷）
- 预算轴：token；当前端点没有可用的美元成本信号
- 执行模式：`live`

## 运行矩阵

| 任务 | Arm | 结局 | Agent 状态 | Prompt 哈希 | 工具步骤 | 计划 | 写入集 | 合并 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 0896629 | L0 | error | error | 15 | 14 | 0 | 0 | 0 |
| 0896629 | L1 | error | error | 43 | 32 | 0 | 0 | 0 |
| 0896629 | L2 | pass | step_limit | 46 | 42 | 4 | 0 | 0 |
| 0896629 | L3 | pass | step_limit | 42 | 40 | 4 | 2 | 1 |
| 60c64ed | L0 | pass | step_limit | 40 | 40 | 0 | 0 | 0 |
| 60c64ed | L1 | fail | step_limit | 40 | 40 | 0 | 0 | 0 |
| 60c64ed | L2 | fail | step_limit | 42 | 40 | 4 | 0 | 0 |
| 60c64ed | L3 | pass | step_limit | 26 | 17 | 4 | 4 | 1 |

8 个 Run Record 均包含模型、prompt 哈希、工具步骤、验证退出码和判决。L2/L3 的
`plan` 均非空；L3 记录含工作区写集与确定性合并证据。L0/L1 的 `plan=0` 符合其
拓扑定义。

## 聚合结果

| Arm | Runs | 有效样本 | Pass | Fail | Budget exhausted | Error | 总 token |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| L0 | 2 | 1 | 1 | 0 | 0 | 1 | 489,392 |
| L1 | 2 | 1 | 0 | 1 | 0 | 1 | 595,331 |
| L2 | 2 | 2 | 1 | 1 | 0 | 0 | 750,332 |
| L3 | 2 | 2 | 2 | 0 | 0 | 0 | 483,042 |

总计为 `4 pass / 2 fail / 2 error / 0 budget_exhausted`。`error` 不进入 pass-rate
分母，也没有被改写成普通失败。token 总量包含 error run 已发生的真实调用，不能当作
“每个有效样本的成本”。

## 错误证据

- `0896629/L0`：模型在第 15 次调用触发
  `finish_reason=length`，`completion_tokens=4096`，最终 content 为空。Run Record
  保留了 14 条工具步骤、累计 reasoning 规模、patch hash 和失败优先验证输出。
- `0896629/L1`：Critic 未产出终局，最后一次调用同样为 `length`；记录标明
  `agent_status=error`、`final_text=Critic 未产出终局`，不会与 harness 的真实 fail
  混算。
- `60c64ed/L1`、`60c64ed/L2`：验证命令真实失败，因此分别记为 `fail`；不是基础设施
  错误。
- `60c64ed/L2` 的 planner 首次调用触发 `length`，协议内重试后得到可解析计划，批次
  继续执行；最终仍按 harness 验证结果记为 `fail`。

## 可复核产物

本地批次产物位于：

```text
.bench-experiments/_batches/m4-pilot-live-r4/
├── batch.json
├── report.json
└── report.md
```

`.bench-experiments/` 被 Git 忽略，是运行证据而不是源码交付。报告中的每个实验 ID
都指向独立、不可覆盖的 Experiment 目录及 `trials/trial-000.json`。

复跑命令：

```powershell
.\.venv\Scripts\python.exe bench/run_batch.py batch `
  --phase pilot `
  --tasks 0896629 60c64ed `
  --arms L0 L1 L2 L3 `
  --mode live `
  --batch-id m4-pilot-live-r4 `
  --out .bench-experiments
```

## 观察与限制

- L3 在两个任务上均为 `pass`，但样本量为 1，不能据此宣称 L3 优于其他拓扑。
- L0 在一个任务上 `pass`、另一个任务 `error`；L1 在一个任务上 `fail`、另一个任务
  `error`。错误与失败已经分开，不能合并成一个“失败率”。
- 当前 Trial 串行执行；批次恢复只跳过完整单元，不支持同一批次内并发 trial。
- L3 已有同路径冲突和不同路径合并的结构化证据，但本轮真实运行没有出现同路径冲突，
  冲突路径由回归测试覆盖。
- `BatchRunner` 尚未实现 B3 自动选择，M6 不能直接用它执行“只复跑 B1 失败档位”。

## 下一步

1. M5：执行 `8 × 4 × 1 = 32 runs`，得到完整单次基线并标出所有缺口。
2. M6：实现 B3 触发选择后执行正式重复实验，每个非缺格至少 3 个有效样本。
3. M7：增加只读结果 UI，所有数字必须能回链到 Run Record。
4. M8：增加并发 trial、全局并发限制和可注入的持久化 checkpointer。
5. M9-M10：再做跨模型、预注册统计、CI 隔离、部署与运行手册。
