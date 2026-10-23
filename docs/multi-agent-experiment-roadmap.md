# 多智能体实验台路线图

> 状态：执行中
> 基线提交：`995e331`（可验证实验台 MVP）
> 上位决策：[ADR-025](adr/adr-025-multi-agent-experiment-run-record.md)
> 使用契约：[multi-agent-experiment-bench.md](multi-agent-experiment-bench.md)

## 目标

把当前“可以真实运行并留下 Run Record”的 MVP，推进到“可以批量执行、隔离并发写、
生成可复核报告、查看结果并受控部署”的完整实验台。

成功不是“多智能体赢了”，而是：

1. 同一任务集、模型和预算下能重复执行 L0-L3；
2. 每个结果能追溯到完整 Run Record；
3. 样本不足、基础设施错误和真实失败不会被混算；
4. 结论能明确区分方向性观察与统计声明；
5. 实验运行、结果查看和生产部署不依赖口头流程。

## 现有事实

- 已有 8 个任务：5 个 E1 历史缺陷与 3 个 E3 合成缺陷。
- 已有 L0-L3 四个 arm、B1/B3 预算策略和聚合报告。
- 已有 `Experiment → Trial → Run Record`、不可变落盘和受控复跑。
- `bench/run_curve.py` 与 `bench/run_experiment.py` 尚未统一成单一权威批量入口。
- 协作图已有 SQLite 持久化 checkpointer；并发专家写同一路径由隔离写集与显式冲突处理。
- 只读结果 UI 已实现；跨模型自动跑分和统计显著性仍未实现。

## 里程碑

### M0：冻结 MVP

状态：已完成（`995e331`）

完成判据：

- 实验台相关改动单独提交；
- 不夹带飞书等用户既有修改；
- 全量单测、集成测试和 live smoke 已通过。

### M1：冻结实验协议

状态：已完成

交付：

- 固定首批 8 个任务与任务来源；
- 固定四个 arm 名称与版本；
- 固定模型、provider、预算轴、B1/B3 上限、重复次数；
- 固定排除规则、错误重试规则和报告口径；
- 明确“方向性结果”与“统计结论”的边界。

完成判据：

- 协议可由机器读取，不靠 CLI 参数口头重复；
- 相同协议重复启动时 `config_hash` 一致；
- 任何协议变化都会产生新的 Experiment，而不是覆盖旧结果。

落地物：

- `bench/protocols/multi-agent-first-batch-v1.json`（历史）
- `bench/protocols/multi-agent-first-batch-v2.json`（历史：1024 输出上限）
- `bench/protocols/multi-agent-first-batch-v3.json`（当前：4096 输出上限）
- `bench/experiment/protocol.py`
- `tests/unit/test_experiment_protocol.py`
- ADR-026

### M2：统一批量入口

状态：已完成

交付：

- 一个 matrix manifest 描述 `task × arm × repeat`；
- 每个矩阵单元映射为独立 Experiment 或 Trial；
- 已完成的单元跳过，不重复花钱；
- 失败单元保留证据并可继续下一项；
- 支持从 manifest 恢复中断批次；
- 导出统一聚合报告。

完成判据：

- 无网络 mock 跑完整个矩阵；
- 中止后再次运行只执行缺失单元；
- 重复执行同一批次不会覆盖已有 Run Record；
- 旧 `run_curve` 能力由新入口覆盖或被明确标记为兼容层。

落地物：

- `bench/experiment/batch.py`
- `bench/run_batch.py`
- `tests/unit/test_experiment_batch.py`
- Experiment 缺失 Trial 恢复与批次级统一报告
- `run_curve.py` 明确标记为兼容分析入口

### M3：工作区写隔离

状态：已完成

交付：

- 每个专家使用独立写集、补丁或工作副本；
- 同一路径的并发写被确定性检测；
- 冲突产生可复核证据，不静默后写覆盖先写；
- 无冲突变更能按确定性顺序合并。

完成判据：

- 两个专家并发写同一文件时实验不崩溃且留下冲突记录；
- 两个专家写不同文件时两份变更都保留；
- L3 回归用例覆盖上述两条；
- Run Record 记录写入者、目标路径、冲突和合并结果。

落地物：

- `app/orchestration/workspace_isolation.py`
- `tests/unit/test_workspace_isolation.py`
- 协作 L3 并行写同文件/不同文件回归用例
- bench L3 依赖波次、独立副本、同路径冲突与不同路径合并回归用例
- Run Record evidence 增加 `workspace_writes`、`workspace_conflicts` 与
  `workspace_merges`
- ADR-027

### M4：工程试点

状态：已完成

交付：

- 首轮 `2 任务 × 4 arm × 1 次 = 8 runs`；
- 修复试点暴露的执行、记录和恢复问题；
- 形成试点报告。

完成判据：

- 8 runs 全部能映射到 Run Record；
- `error` 与 `fail` / `budget_exhausted` 分开；
- 任一 run 可从记录追回模型、prompt、工具轨迹和判决。

落地物：

- `m4-pilot-live-r4`：8 / 8 单元完成，0 缺口；
- 4 个 L2/L3 Run Record 均含实际计划，L3 另含工作区写集与合并证据；
- `docs/multi-agent-pilot-report.md`

说明：`m4-pilot-live-r3` 因 L2/L3 未记录实际计划和步骤轨迹而作废，由 r4 取代，
不作为能力证据。

### M5：单次全量基线

状态：已完成

交付：

- `8 × 4 × 1 = 32 runs`；
- 按任务来源、arm、结局、token 和成本聚合；
- 标注所有缺口与基础设施错误。

完成判据：

- 每格都有结果或显式缺口；
- 报告重算确定；
- 不以单次通过率宣称统计优势。

落地物：

- `m5-baseline-live-r1`：32 / 32 单元完成，0 缺口；
- 32 条 Run Record：15 `pass`、8 `fail`、9 `error`、0 `budget_exhausted`；
- `docs/multi-agent-baseline-report.md`；
- 报告连续重算哈希一致：`C8ABB6A7...E02BD`（JSON）与
  `8EAD2D4E...0DE26`（Markdown）。

说明：M5 发生在协议声明的错误重试真正接入执行器之前，因此 9 个 `error` 都是首次
attempt。该缺口已单独修复并以单测锁定；M5 只作为单次工程基线，正式比较以 M6 的
带重试证据为准。

### M6：正式重复实验

状态：执行中

交付：

- 协议冻结后执行 `8 × 4 × 3 = 96 runs`；
- B3 只对 B1 的失败/预算耗尽档位复跑；
- 报告包含描述性区间、样本不足标记和任务来源拆分。

完成判据：

- 每个非缺格至少 3 个有效样本；
- `under_sampled`、`gaps` 和 `error` 都可见；
- 结论明确区分“方向性”与“统计显著”。

当前进展：

- `run_batch.py b3` 已实现从完整 B1 批次派生失败/预算耗尽档位；
- B3 使用独立 Experiment 身份、B3 预算和 B1 基线 cost/token；
- 离线选择、CLI、来源校验和报告分层已有单测；
- 正式 `8 × 4 × 3` B1 与派生 B3 尚未执行，因此 M6 未完成。

### M7：报告与只读 UI

状态：执行中（只读 UI 已实现，正式数据验收待 M6）

交付：

- 只读结果页：Experiment 列表、批次状态、Trial/Run Record 详情；
- 报告页：通过率、token、成本、预算耗尽、错误、缺口；
- 不提供从 UI 修改执行语义的入口。

完成判据：

- UI 数字与 JSON 聚合结果逐项一致；
- 任一图表可回溯到 Run Record 文件；
- 页面明确显示样本量和不可下结论的原因。

当前进展：

- Experiment 列表、批次状态、Trial/Run Record 详情与只读 API 已实现；
- 批次页显示样本量、有效通过率、`error`、预算耗尽、平均 token、平均成本；
- 并发上限、启动速率和协议哈希读取自持久化 Manifest；
- 正式 96 条 B1 报告生成后，再执行 JSON/UI 对账与桌面/移动端验收。

### M8：并发 Trial 与持久化恢复

状态：已完成

交付：

- 并发 Trial 使用独立工作区和独立 Experiment ID；
- 全局并发上限、速率限制和取消语义；
- 协作图可注入持久化 checkpointer；
- 重启恢复不会伪造已完成记录。

完成判据：

- 并发任务不共享可变工作区；
- 进程重启后批次可继续且不覆盖旧记录；
- HITL 挂起在进程重启后仍可恢复或明确失效；
- 并发与串行结果在相同配置下走同一证据契约。

落地物：

- `BatchRunner` 在未完成 Experiment 间并发调度，全局信号量限制在途 Trial；
- `max_concurrency` 与 `rate_limit_per_minute` 写入 Batch Manifest，恢复时拒绝漂移；
- 取消会保留已写 Run Record，并把未完成单元与批次恢复为 `paused/pending`；
- SQLite `AsyncSqliteSaver` 通过 `COLLAB_CHECKPOINT_PATH` 持久化 HITL；
- `apending_payload()` / `afinish()` / `aclose()` 异步生命周期与 lifespan 关闭；
- 并发闸门、速率门、并发/串行证据契约、独立子进程 HITL 恢复测试；
- ADR-028。

### M9：跨模型与统计扩展

状态：待完成

交付：

- 多模型矩阵；
- 预注册样本量与主判据；
- 配对任务级比较、区间估计和敏感性分析。

完成判据：

- 只有达到预注册样本量后才输出统计显著声明；
- 模型切换、供应商错误和价格不可用被显式记录；
- 不以免费端点缺失的美元成本伪造“零成本优势”。

### M10：部署与交付

状态：待完成

交付：

- 固定迁移与依赖检查；
- 实验服务只读结果入口；
- 文档、CI 和运行手册同步；
- 合并与部署验证证据。

完成判据：

- 新环境按文档可启动；
- CI 不访问真实模型；
- live 实验明确手动触发；
- 部署后可查看至少一轮已完成实验。

## 执行顺序

M1 → M2 → M3 → M4 → M5 → M7（代码收口）→ M8 → M6 → M7（正式数据验收）→ M9 → M10。

M7 必须读取稳定的 Run Record 和聚合报告，不得先于 M2/M3 固化错误语义。
M9 不得用当前 8 个任务的单次结果替代预注册实验。
并发与持久化恢复属于正式实验的执行契约，因此 M8 必须在 M6 大规模 live 批次前冻结。
