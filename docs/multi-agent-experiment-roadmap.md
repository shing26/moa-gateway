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
- 只读结果 UI、跨模型描述矩阵和预注册统计门禁已实现；本地第二模型正式批次
  已完成 96 条，NVIDIA 第一模型确认性批次仍受端点容量阻塞。

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
- `bench/protocols/multi-agent-first-batch-v3.json`（历史：4096 输出上限）
- `bench/protocols/multi-agent-first-batch-v4.json`（当前：预注册配对统计门禁）
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
- 报告连续重算哈希一致：`C5B703C8...3E7A`（JSON）与
  `CB8C35C5...4067`（Markdown）。

说明：M5 发生在协议声明的错误重试真正接入执行器之前，因此 9 个 `error` 都是首次
attempt。该缺口已单独修复并以单测锁定；M5 只作为单次工程基线，正式比较以 M6 的
带重试证据为准。

### M6：正式重复实验

状态：阻塞于上游端点容量

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
- `m6-formal-b1-live-r1` 在并发 4 下尚未完成任何 Trial 即进入持续 429，已暂停并作废，
  不作为能力证据；
- `m6-formal-b1-live-r2` 改为并发 1、30 次启动/分钟；运行约 46 分钟后只落下 3 条
  Trial（pass 1、fail 2），32 个单元仍为 0 个完整单元；最后一条 Trial 后约 25 分钟
  持续 429，已暂停并保留为容量阻塞证据；
- 2026-10-23 10:27 使用同一 NVIDIA 模型完成 8-token 容量探测，返回 `HTTP 200`；
  但短探测不足以证明长调用容量；
- v4 `m6-m9-formal-b1-v4-live-r1` 以并发 1 完成 3 条 Trial（L0 pass、L1
  budget_exhausted、L2 pass）后再次进入持续 429，已暂停；
- v4 `m6-m9-formal-b1-v4-live-r2` 以并发 4 启动后立即连续 429，0 条 Trial，
  已暂停；不能把这两个批次算作正式 arm 比较；
- 2026-10-23 11:36 连续 4 次 512-token 长调用均返回 `HTTP 200`，但恢复 v4 r1 后
  第一次真实 agent 调用立即再次 429；runner 已停止，批次重新置为 paused，证明
  探测成功仍不能替代持续容量；
- 正式 `8 × 4 × 3` B1 与派生 B3 尚未完成，因此 M6 未完成。

下一步：

1. 等账户级容量稳定后恢复 v4 r1，不要用已暂停批次冒充正式样本；
2. 正式 B1 达到 96 条连续 Run Record 后，才允许从其最终 `fail` /
   `budget_exhausted` 单元派生 B3；
3. 若改用其他模型，必须生成新的协议版本和 `protocol_hash`，不能把跨模型结果混入
   当前 v4 批次。

### M7：报告与只读 UI

状态：已完成

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
- 最终报告缺失时从已落盘 Trial 文件计算进度；`running` 只标记真正持有执行槽的
  cell，排队中的 cell 保持 `pending`；
- 移动端表格已改为带字段名的记录块；桌面与 390px 视口均无横向溢出、控制台错误或
  框架遮罩，批次链接与 Run Record 链接可交互（`7fe42ca`）；
- 新增 `scripts/verify_experiment_batch.py`，从不可变 Run Record 重建完整报告，
  并核对 `report.json`、报告来源路径、Trial 序号和只读 API 返回；篡改报告回归
  用例会拒绝通过；
- M5 的 32 条记录已通过新的确定性对账脚本：重建报告与 `report.json` 一致，
  32 条 Run Record、来源路径和只读 API 返回全部核对通过；桌面与移动端另完成
  页面抽样验收；
- 本地正式批次 `m9-local-qwen25-3b-formal-b1-live-r1` 的 96 条 Run Record
  也通过同一脚本全量对账；`report.json` SHA256 为
  `5D004F8B1E16466B13EA0CF82ED9EF3FDBCCF2092D7801DD3589CC78CD16A05C`；
- 正式 96 条 B1 已完成 JSON、只读 API、Run Record 全量对账；容器中的桌面
  1440px 与移动端 390px 均无空白、横向溢出、控制台错误或页面异常。

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

状态：部分完成；本地第二模型正式批次已完成，阻塞于 NVIDIA 第一模型确认性数据

交付：

- 多模型矩阵；
- 预注册样本量与主判据；
- 配对任务级比较、区间估计和敏感性分析。

完成判据：

- 只有达到预注册样本量后才输出统计显著声明；
- 模型切换、供应商错误和价格不可用被显式记录；
- 不以免费端点缺失的美元成本伪造“零成本优势”。

当前进展：

- 新增 `multi-agent-first-batch-v4.json`，固定 `L3 - L0`、8 个配对任务、每格 3 次
  和至少 3 个有效样本；
- 新增确定性任务级 paired bootstrap、精确符号翻转检验、
  leave-one-task-out 与来源分层敏感性分析；
- 多模型输入必须显式选择单一模型才可做确认性比较；
- `model × arm` 矩阵把无成本信号标为 `unavailable_or_zero` 或
  `mixed_or_unpriced`，不冒充零成本；
- `BatchReport` 分别写出 `analysis_authorized` 与 `statistical_claims`：前者表示
  预注册分析可执行，后者只在得到 `candidate_better` / `candidate_worse` 时为真；
- `run_batch.py compare` 可读取多个已完成且矩阵一致的独立模型批次，输出
  `model × arm` 描述矩阵，并在每个模型内分别执行预注册配对分析；
- 跨模型报告固定 `cross_model_inferential_claim=false`，不合并模型总体通过率；
- 新增独立协议 `multi-agent-first-batch-v5-local.json`，固定本地
  `qwen2.5:3b`、Ollama OpenAI 兼容端点和独立 `protocol_hash`
  `efa76c90...4e9b5`；
- v5 pilot `m9-local-qwen25-3b-pilot-live-r1` 完成 `4/4`：L0/L1/L2/L3 均为
  真实 `fail`，0 `error`；四条 Run Record 均含模型调用与工具轨迹；
- v5 正式批次 `m9-local-qwen25-3b-formal-b1-live-r1` 完成 `96/96`，
  32 个单元全部完成，0 error；四个 arm 各 24 个有效样本，通过率均为 0%；
- 预注册分析门禁已通过并给出 `no_significant_difference`：L3 - L0 平均配对差
  `0.0`，95% 区间 `[0.0, 0.0]`，符号翻转 `p=1.0`；这不是 L3 优于 L0 的证据；
  该批次 `analysis_authorized=true`，`statistical_claims=false`；
- ADR-029 与统计单测已落地。

剩余：端点容量恢复后，用 v4 协议完成 NVIDIA 第一模型确认性 live 批次；只有它和
本地 v5 正式批次都完成后，才能生成有真实数据的跨模型报告。不能用 v3 B1 批次
回写成 v4 结论，也不能把 M5 的单次方向性曲线升级为统计声明。

### M10：部署与交付

状态：已完成（单机 Compose + Tailnet 私有入口）

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

当前进展：

- `uv lock --check`、Ruff、依赖锁定检查、离线 eval 和 redteam 基线已通过；
- 完整回归为 `1629 passed, 21 skipped`（641.52 秒）；
- 启动仓库测试 Postgres/Redis 后，集成与契约回归为 `37 passed, 16 skipped`；
- Bandit 的固定命令子进程低危发现已按真实边界标注，CI 命令现在通过；
- `bench.report` / `bench.experiment` 的双向导入顺序已由独立进程测试锁定
  （`45ba01e`）；
- Compose 网关已按当前代码重建；端口由 `.env` 的 `GATEWAY_PORT` 控制，当前为
  `http://127.0.0.1:8083`，实验目录以只读卷挂载；
- Tailnet 私有部署入口 `http://100.99.204.104:8083` 已实测返回 `HTTP 200`；
- 部署后 smoke 已验证 M5 的 `32/32`、Run Record 详情和运行中 v4 批次进度；
- 2026-10-23 11:23（Asia/Shanghai）复验容器 `moa-gateway-gateway-1` 正常运行，
  本机与 Tailnet 的 `/dashboard/api/experiments` 均返回 `HTTP 200`；
- 2026-10-23 13:11 容器在长批次期间曾退出，已重启并重新验证本机与 Tailnet；
  两者都能读取本地正式批次的 `96/96`、0 缺口和只读统计结果；
- 2026-10-23 14:36（Asia/Shanghai）用当前代码重建镜像并重启；本机与 Tailnet
  均能从 `8083` 读取 `96/96`、0 缺口、`analysis_authorized=true` 和
  `statistical_claims=false`，页面明确提示未授权显著差异；
- 发布证据批次为 `m5-baseline-live-r1`，`report.json` SHA256 为
  `C5B703C8AEAED5766A52BD9635E4F5CB9DD3EEC3C50EFE722112E906B77B3E7A`；
- 桌面 1440px 与移动端 390px 重新验收通过：无空白、横向溢出、框架遮罩、
  控制台错误或页面异常，批次页与 Run Record 链接可交互；
- 当前交付是受 Basic Auth 保护的单机 Compose + Tailnet 私有入口；公网 Funnel
  尚未切到实验台，不能把本地或 Tailnet 地址冒充公网部署。

## 执行顺序

M1 → M2 → M3 → M4 → M5 → M7（代码收口）→ M8 → M6 → M7（正式数据验收）→ M9 → M10。

M7 必须读取稳定的 Run Record 和聚合报告，不得先于 M2/M3 固化错误语义。
M9 不得用当前 8 个任务的单次结果替代预注册实验。
并发与持久化恢复属于正式实验的执行契约，因此 M8 必须在 M6 大规模 live 批次前冻结。
