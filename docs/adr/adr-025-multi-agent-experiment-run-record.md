# ADR-025: 多智能体实验以 Run Record 为验收契约

日期：2026-10-21
状态：已接受

多智能体实验台的首版验收面是一条真实可运行的协作链路，以及该次运行产出的机器可读
Run Record；Web UI 和横向跑分是后续消费者，而不是首版前提。数据模型固定分为
`Experiment → Trial → Run Record`：Experiment 固定任务与配置，Trial 是一次独立执行，
每个 Trial 产出一条 Run Record。Run Record 必须足以复核任务、实际计划与依赖、模型与参数、
提示词与工作区快照标识、工具调用、预算与耗时、Critic/Guard/HITL 结果和最终状态。
这里的“可验证”定义为受控复跑：从同一配置与工作区快照重新执行，并比较可观察行为是否偏离；
模型输出允许自然变化，不承诺逐 token 重放。

## Considered Options

- **逐 token 确定性重放**：需要把所有模型输出当作回放脚本，只能证明录制结果可再播，
  不能证明系统在新一次运行中仍具备该能力。保留为离线回放能力时，不得称为实验复现。
- **只保存最终答案与日志**：成本低，但无法区分计划错、工具错、判决错还是模型文本波动，
  不满足“可验证实验台”。

## Consequences

- 新运行必须记录足够的配置与快照标识；缺失证据时应明确标为不可复验，而不是猜测补齐。
- 两次受控复跑不会自动视为失败；比较的是可观察行为与判决，偏离本身是实验结果。
- 首版可以只跑 N=1 的 Trial 完成验收，但数据模型不得假设 Experiment 只有一条记录。

## 首版实装（2026-10-22）

- `bench/experiment/` 实现 `Experiment → Trial → Run Record`。Run Record 写入后不可变；
  `experiment.json` 仅作为进度索引，只允许追加 Trial 进度，配置、身份与既有 Trial
  前缀均拒绝改写。
- Run Record 固化任务、模型、运行配置、验证命令、实际计划、依赖路径、治理证据、资源账本
  和工作区执行前后快照。
- `LLMClient` 对每次真实请求的 messages 与 tool schemas 生成稳定 `prompt_hash`；协作与
  bench 账本把整次 Trial 的请求摘要收进 Run Record。摘要不包含模型名、密钥或绝对路径。
- `CollaborationExperimentAdapter` 与 `BenchExperimentAdapter` 只映射统一证据，不合并
  两套执行引擎。
- `bench/run_experiment.py` 提供 mock/live 实验、bench L0-L3、受控复跑比较三个入口。
  Trial 数不一致、配置变化或工作区起始内容变化会显式报告 divergence。
- 使用说明与 schema 见 `docs/multi-agent-experiment-bench.md`。
