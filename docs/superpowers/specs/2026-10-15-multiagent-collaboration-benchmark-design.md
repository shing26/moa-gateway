# 多智能体协作剂量曲线实验 · 设计

- 日期：2026-10-15
- 状态：设计已确认，待实现
- 修订：2026-10-17（§5.0 落地时按实测修正三处口径，见 §13）
- 相关：ADR-022（多 Agent 协作）、`app/orchestration/collaboration.py`、
  `scripts/compare_orchestrators.py`（诚实测量的先例）

## 1. 背景

项目定位发生过漂移。初衷是做一个**成熟的多智能体协作系统**，但因为缺少具体方向，
被引导成了 **PR 审查 agent**。结果是：一套相当扎实的**水平治理底座**（FSM/LangGraph
双运行时、审计哈希链、HITL、队列崩溃恢复、合并状态机）外面，包了一个**垂直应用**，
而这个应用根本用不上协作——PR 审查是一条顺序流水线，不需要 agent 之间协商、分工、
互相纠错。最强的资产被放在了不需要它的场景里。

仓库里其实有两条"多 Agent"线，它们不是一回事：

- `apps/code_review_pipeline/agents/` 的 5 个 agent（triage / static / semantic /
  test_coverage / report）在 `code_review_pipeline.py` 里是**一个 `await` 接一个
  `await` 的顺序流水线**，没有并发、没有协商。它是"5 个专用 LLM 调用"，不是协作。
- `app/orchestration/collaboration.py`（ADR-022）是**真协作**：Supervisor 规划 →
  `Send` 扇出并发 → Critic 反思 → 有界 revise 回边。但它**默认关闭、不进主路径**，
  且跑的是通用聊天任务，不接任何真实产品场景。

所以本项目当前形态是：一个够用的协作引擎 + 一个够用的 PR 审查应用，缺的是**让它们
成为同一件事的理由**。本次工作不加新功能，而是给项目一个**能回答问题的实验**。

## 2. 目标

- **首要目标（D · 学习）**：激活并调试多智能体编排。通过跑实验，被迫把编排的每一块
  都亲手碰一遍——扇出、reducer 通道、并发写收敛、Critic 回边、预算分配。
- **次要目标（A · 作品集）**：产出一个**有证据、可复现**的作品集条目。

作品的形态不是"一个多智能体系统"，而是**"一个能回答某个问题的实验平台 + 一个站得住
的结论"**。PR 审查从"产品"降级为"测试床的一类任务"，`collaboration.py` 从"旁路"
升为"研究对象"。现有的治理层（有界、可审计、可恢复、成本可测）正好是让这个实验
**可信**的东西。

### 2.1 定位修正（2026-10-15 追加）

原项目从"能力"向外堆，而不是从"问题"向内建，因此长成了四不像。两个标签都不成立，
必须从叙事里删掉：

- **"网关"不成立**：网关的职责是路由/代理/治理流量，而本项目在自己干活（跑审查、
  合并 PR、管状态），它是一个应用服务器，不是一个网关。
- **"多智能体系统"不成立**：真正的 agent 需要**工具、循环、目标**三样，能作用于环境
  并观察结果。而现有 `code_review_pipeline` 的 5 个 agent 是**顺序 LLM 调用**（无循环、
  无工具），`collaboration.py` 的专家是**聊天型**（只输出字符串，不能读写文件、跑测试）。
  它们是"提示词 + 一次调用"，不是 agent。

**修正后的定位**：这是一个**关于多智能体协作边界的研究项目**，核心资产是
**"让 AI 对不可逆的生产操作负责"**（sha 乐观锁、幂等写回、审计哈希链）以及**一个可
复现的等预算对比实验**。

### 2.2 实施顺序：先把原语做对

**agent 是原语，梯子只是"怎么组合这个原语"。** 现有项目的根本问题是没有这个原语，
直接从"协作"往上搭。因此实施顺序是：

1. **先做一个真 agent**（见 §5.0），它是核心可交付物，单独成模块。
2. 再把它组合成 L0–L3 四档，跑剂量曲线。

### 2.3 降级项

网关主路径（Redis 队列 / HITL / 飞书卡片 / 合并通道）**降级为"我建过、本次不用"**。
代码不删，但不再作为叙事或实验的一部分——它们与"编排结构"无因果关系，只会加噪音。

## 3. 设计决策

| 维度 | 选择 | 理由 |
|---|---|---|
| 命题 | **T1 · 对比命题**：协作在什么任务上真的比单体强，强多少，代价多大 | 唯一一个"问题本身有意思、答案能用数字说话、且把已有资产全变成论据"的选项 |
| 任务形态 | **S1 · 可执行验证**：改 bug → 测试由红变绿 | 二值事实，噪音最低，不需要辩护；天然支持难度梯度 |
| 任务来源 | **E1 + E3**：自己仓库的历史 bug 回放（锚点）+ 合成缺陷注入（梯度） | E1 真实可信、天然有 ground truth；E3 把难度往两端延伸，证明边界不是任务集太窄造成的 |
| 对比规则 | **B1 为主 + B3 为辅**：等预算为主，能力上限为辅 | B1 直接回答"值不值"且作弊不了；B3 补充"协作有没有独特能力" |
| 算力 | **C2 · API 中档模型** | S1 对模型能力有硬门槛，弱模型会让两条线都失败；且成本轴要是读者一眼能懂的单位（美元） |
| 实验路线 | **路线二 · 剂量曲线** | 把编排结构本身当自变量，跑出一条"每加一个 agent 的边际收益"曲线；学习发生在调通每一档的过程中 |

**结论形态**：不是"协作更好"，而是**"协作的边际收益在第几个 agent 上转负，拐点在哪、
为什么"**。这个"边界"结论比"协作更强"这种口号值钱得多。

## 4. 架构

### 4.1 关键决定：独立 harness，不走网关主路径

实验跑独立 harness，绕开 HTTP / Redis 队列 / HITL / 飞书 / 合并通道。理由是**变量要
干净**——主路径上的那些组件与"编排结构"没有因果关系，只会给对比加噪音。harness 直接
import `orchestration` + `agents` + LLM 层。

等曲线跑出来，再考虑把胜出的档位接回网关；那是后续工作，不在本次范围。

### 4.2 代码落位

```
bench/
  tasks/           任务定义：每个任务 = 一个 git 基线 + 验证命令 + 难度档 + 来源(E1/E3)
                   + 两个 patch（<id>.test.patch / <id>.solution.patch，由修复 commit
                   按 tests/ 路径拆开；运行期不依赖原始 commit 内容）
  agent/           §5.0 的 agent 原语：工具 + 循环 + 目标 + 记忆 + 预算
  harness/         任务运行器：物化工作区 → 跑 arm → 恢复测试文件 → 验证 → 记录
  arms/            L0-L3 四个档位，实现同一个接口 run(workspace, goal, budget) -> AgentRun
                   （判决 RunResult 一律由 harness 组装，不由各档产出）
  report/          聚合与出图：通过率曲线 + 成本曲线 + 边际收益
```

放在 `bench/` 而不是塞进 `evals/`：`evals/` 是"给仓库自己打分的回归集"，`bench/` 是
"回答一个研究问题的实验台"，两者生命周期不同，混在一起会让 `evals/` 的语义变糊。

### 4.3 数据流

```
任务定义
  → 物化一个干净工作区（git archive 基线 → 建 orphan 单提交快照，每个任务一个）
  → fail-first 检查：先跑一次验证，必须失败（否则剔除该任务）
  → arm.run(workspace, goal, budget) -> AgentRun   ← 唯一变量在这里
       agent 循环：读文件 / 写文件 / 跑测试
  → 采集 patch（含 agent 对测试文件的改动，那是证据）
  → 整棵测试树还原到 test patch 版本      ← 防作弊闸门
  → 跑该任务固定的验证命令
  → RunResult{ outcome, agent_run(自报: status/steps/usage/model),
               patch, verdict_exit_code, wall_clock_s, patch_touched_tests }
→ 跨 任务 × 档位 聚合 → 曲线 + 报告
```

### 4.4 复用现有资产

- **L2/L3 骨架**：`app/orchestration/collaboration.py`（Send 扇出、reducer 通道、
  Critic 回边、`COLLAB_MAX_ROUNDS`）。要做的是把它从"通用聊天任务"接到"代码任务 +
  工具"上。
- **记账**：`app/agents/provider.py` 的 `LLMClient.last_metrics`（已有 `cost_usd` /
  tokens / latency 通道）。
- **报告**：沿用 `evals/reports/` 的写法。

### 4.5 隔离

每个任务一个独立工作区，agent 的写操作不跨任务泄漏；验证在干净的工作区副本上跑。

## 5. 四档编排

四档实现同一个接口 `run(workspace, budget, goal) -> AgentRun`，**唯一变量是编排拓扑**。
返回值是**自报**（我为什么停），判决 `RunResult` 一律由 harness 在跑完验证后组装
（ADR-023 与 §8）。
下列全部共享、四档一致：同一个模型、同一套工具（`read_file` / `write_file` /
`list_files` / `search` / `run_tests`）、同一段任务指令、同一个停止条件（agent 声明
完成 / 预算耗尽 / 步数上限）、同一个账本。

### 5.0 agent 原语（先做对，再谈组合）

`bench/agent/` 是核心可交付物。一个真 agent 必须有：

| 要素 | 说明 |
|---|---|
| 工具 | 读文件、写文件、列目录、搜代码、跑测试 |
| 循环 | 观察 → 决策 → 行动 → 观察，有步数上限 |
| 目标 | 让失败的测试变绿 |
| 记忆 | 知道自己已经试过什么，不重复撞同一面墙 |
| 预算 | 每次调用记账，超了硬停 |
| 产出 | 一个 diff + 一条行动轨迹 |

**最关键的一条：它必须能失败。** 现有"agent"永远在"成功"——读 diff、吐 JSON、任务
结束，没有失败状态；那不是 agent，是格式化输出器。真正的 agent 会试错、跑测试、发现
还红、换思路、卡住、重试，最后可能放弃。**"会不会卡住、卡在哪"正是要测的东西**——若
agent 不会失败，剂量曲线测不出任何东西，因为所有档位都"通过"。

**第一个可交付物**：一个能读代码、改代码、跑测试、能失败、能记账的单 agent，在仓库里
一个真实历史 bug 上把测试从红跑成绿——**或者诚实地卡住**。

| 档位 | 结构 | 要碰到的编排动作 |
|---|---|---|
| **L0** | 单体 ReAct 循环（§5.0 的 agent 跑一份） | 基线。工具循环、停止条件、预算记账 |
| **L1** | 单体 + Critic | 回边、Critic 反馈如何转成可执行指令、有界 revise |
| **L2** | Planner + 1 worker + Critic | 任务分解、子任务 handoff、上下文在规划者/执行者之间怎么传 |
| **L3** | Planner + N worker 并发 + Critic | `Send` 扇出、reducer 通道、并发写收敛、单点裁决、按子任务定向重跑 |

### 5.1 L2/L3 的专家需要工具

`collaboration.py` 里的 `coder` / `general` / `review` 是**聊天型 agent**（输出字符串），
不是能读写文件、跑测试的执行体。要让它们干活，必须给专家池接上工具循环——这是本实验
**最大的一块真实工作量**，也是多智能体编排最核心的一课：**agent 不只是提示词，还得有
行动能力**。实现前先查 `app/agent_core/task_agent.py` 是否已有可复用的工具循环。

**落点（2026-10-17 实现决定，见 §13.2）**：专家做在 `bench/agent/expert.py`，不在
`collaboration.py`——`app/` 零改动（ADR-023 决策 1）。复用的是循环机制
（`bench/agent/loop.py` 的原生 tool-calling），不是 `ReActLoop` 的 JSON-in-text。

### 5.2 L3 的并发写收敛

多个 worker 在同一 superstep 并发写同一个通道会直接 `InvalidUpdateError`（README 记录
了第一版正是这么崩的）。L3 的正确形态：并发 worker 只写**带 reducer 的通道**
（`failure_reasons` / `outputs`），"是否升级、升成什么"由下游**单个**节点裁决。

### 5.3 预算规则（对应 B1 + B3）

- **主实验（B1 · 等预算）**：四档给**同一个预算上限**。L3 可能因规划开销把预算烧光而
  失败——**那不是 bug，是发现**（协调开销是协作成本的一部分）。
- **补充（B3 · 上限）**：主实验里失败的档位再给**放宽的预算**跑一遍，回答"协作能不能
  做到单体无论如何做不到的事"，并诚实标注它多花了多少。

**预算上限的单位是美元（`cost_usd`），不是 token 数。** 四档共用同一个美元上限，因为
B1 要回答的是"同等花费下谁更强"；token 数只是成本的一个来源，用 token 当上限会让
"多 agent 但每 agent 更省"这类情况无法比较。token 上限作为**兜底**保留（防止单个
agent 无节制地刷 token），但它不是主判据。

**降级路径（2026-10-18 修订）：主轴可切到 token。** 上面"单位是美元"有个前提——
provider 报得出成本。免费/无定价端点（实测 NVIDIA integrate API 的 nemotron）返回的
`cost_usd` 恒为 0，`litellm.completion_cost` 直接抛 `This model isn't mapped yet`；
美元这条判据永远不触发，"等预算谁更强"根本无从算起。此时把主轴切成 token：四档共用
同一个 **token** 上限，美元这条不设（`BudgetPolicy(axis="tokens")` 把 `max_cost_usd`
置为 `inf`），token 成为唯一可比的货币。这是**降级不是替换**——有定价的模型仍按美元
跑。配套两条：报告同时输出美元与 token 两列，成本列全 0 时点破"模型无定价信息，
请看 token 列"；B3 的"多花了多少"在 token 主轴下按增量 token 记
（`RunResult.incremental_tokens`）。

### 5.4 核心结果表

```
             L0      L1      L2      L3
简单任务     ✓/成本   ✓/成本  ✓/成本  ✓/成本   ← 预期：协作无收益甚至更差
中等任务     ...
困难任务     ...                              ← 预期：协作开始体现价值
```

每一列减去前一列 = **"每加一个 agent 的边际收益"**，即剂量曲线。

## 6. 指标

每个 run 记录：

| 类别 | 字段 |
|---|---|
| 判据 | `passed`（验证命令 exit 0）、`budget_exhausted`、`error` |
| 成本 | `tokens_in/out`、`cost_usd`、`wall_clock_s`、`llm_calls` |
| 结构 | `agent_count`、`revise_rounds`、`subtask_count` |
| 证据 | `diff`、`steps`、`model`、`temperature`、`task_id`、`arm_version` |

派生指标：边际收益（Δpass / Δcost per tier）、效率（pass per $）、协调开销占比。

## 7. 防作弊

这是 S1 能不能立住的全部：

1. **fail-first 检查**：物化工作区后先跑一次验证，**必须失败**。不失败说明任务本身是
   坏的（代码没坏 / 测试没覆盖），直接剔除。没有这一步，后面所有数字都可能是"任务本来
   就是绿的"。
2. **测试文件只读**：验证前把测试文件强制还原到 **test patch 版本**（不是基线版本——
   E1 的测试通常与修复同 commit 新增，基线里根本没有这条判据，还原到基线等于把判据
   删掉），并删除 `tests/` 下新增的未跟踪文件。agent 改了也没用。
3. **验证命令固定**：写在任务定义里，不由 agent 决定。
4. **不给网络**：工具集里没有联网/装包，`run_tests` 在受限环境跑，避免 agent 下载一个
   会过的测试。
5. **diff 全留**：记录完整改动，人能事后核查它是真修了还是绕过了。
6. **剥掉 git 历史**：E1 任务的修复**就在仓库的 git 历史里**。若工作区带 `.git`，
   agent 可以直接 `git log` / `git show` 把答案抄出来。任务工作区必须是**无 `.git` 的
   干净副本**（或用 orphan 快照），否则整个实验在第一道题上就废了。
7. **E1 任务拆成两个 patch**：修复 commit 按 `tests/` 拆成 test patch（判据）与
   solution patch（答案）。工作区 = 基线 + test patch，fail-first 必须失败，agent
   只需产出 solution 那部分。实测五个 E1 候选**全部是"修复 + 测试"同 commit**，
   不拆 patch 就没有可用的判据。

## 8. 错误处理

必须区分三种失败，混在一起是这类实验最常见的塌方方式：

- `fail`：验证没通过——这是**结果**。
- `budget_exhausted`：预算烧完——这是 B1 的**核心信号**，必须和 `fail` 分开，否则
  "协作太贵"会被误读成"协作不行"。
- `error`：API 限流 / 超时 / 崩溃——这是**噪音**，单独统计、不混进通过率。

三者的**归属不同**，这是"执行体不能给自己下判决"在类型上的落点：`fail` 是判决
（`RunResult`，只能由 harness 在跑完验证后填），`budget_exhausted` / `error` 是自报
（`AgentRun`）。混合的代价是审计时说不清 `passed: true` 是谁说的。

其他：验证命令超时记为 `fail`（不是 crash）；agent 死循环由步数上限兜底。

## 9. 可复现与统计诚实

- LLM 是随机的，单次结果不可信：每个 (任务 × 档位) 跑 **3 次**，报通过率与方差。
- 样本量小就明说。n=3 时"100% 通过"没有意义，报告里不许出现这种话。
- 结果一 run 一 JSON，报告用固定模板生成，可重跑复现。

## 10. 预算

20 题 × 4 档 × 3 次 = 240 个 run，加 B3 补充轮次约 300 个 run。中档模型下每个代码任务
几万 token 量级，粗估**几十美元**。题量砍到 12 题可压到十几美元。

对"学习编排"这个首要目标，**12-20 题足够把四个档位都调通**——学习发生在调通 L3 的
过程中，不发生在第 30 道题上。不要一上来就追求统计显著。

## 11. 边界与不做的事

- **不做**公开 benchmark（E2，如 SWE-bench Lite）：等系统在这个尺度上证明自己再上。
- **不接**网关主路径（队列 / HITL / 飞书 / 合并通道）：变量要干净。
- **不追求**统计显著：首要目标是学习，题量服从学习而非显著性。
- **不碰** `collaboration.py` 的治理语义（guard / HITL / 审计）：实验不改变既有治理行为。

## 12. 待定项

- **E1 候选任务**（2026-10-15 从 git 历史挑出，均满足"行为级测试 + 修复收敛 + 验证命令
  明确"；2026-10-17 复验：五个候选**全部**是"修复 + 测试"同 commit，必须拆 patch；
  `988fccb` 的 fail-first 已由 `tests/unit/test_bench_harness.py` 的端到端用例实测
  通过，并已落成 `bench/tasks/988fccb.*`，作为首个任务）：

  | 难度 | commit | 内容 | 修复面 |
  |---|---|---|---|
  | 易 | `a746b81` | GitHub 调用默认绕过系统代理 | `github_client.py` 19 行 |
  | 易 | `0896629` | 飞书富文本(post)解析失败 → "发了没反应" | `feishu_event.py` 63 行 |
  | 中 | `988fccb` | `/knowledge` 与 `/api/v1/privacy` 完全绕过鉴权 | `auth.py` 11 行 |
  | 难 | `bd4f186` | 审计链"每文件一条"→"每写者一条" | `audit/wal.py` 56 行 |
  | 难 | `60c64ed` | tool_calls 改尝试数口径 + 降级留痕 | `react.py` 等 3 文件 |

  首推 `988fccb`：安全洞、影响具体（匿名写向量库 / 按 user_id 删数据）、修复收敛、
  测试行为级（断言 HTTP 状态码，不泄漏实现）。
- **E3 合成缺陷规格**（2026-10-18 落定）：见
  `docs/superpowers/specs/2026-10-18-e3-synthetic-defects.md`。首批三条任务
  （`e3-c1-*` / `e3-c2-*` / `e3-c3-*`）已用 fail-first 筛过，任务定义里带
  `source: "E3"` 与 `fix_surface`，报告按来源区分 E1/E3。构造细节与剔除候选见
  `.scratch/bench-agent/issues/10-e3-synthetic-defects.md`。
- 每题预算上限的具体数值需按真实预算反推（真实模型 smoke 已跑：8 次调用烧
  57,712 prompt tokens，可据此反推 token 上限）。
- ~~中档模型的具体选型未定~~（2026-10-18 定）：`nvidia/nemotron-3.5-super-vl-120b-a12b`
  @ `https://integrate.api.nvidia.com/v1`。**免费模型**，`cost_usd` 恒为 0，因此按
  §5.3 的降级路径走 **token 主轴**。原生 tool-calling 实测可用。

## 13. 修订记录（2026-10-17，§5.0 落地）

按实现时的实测修正了三处口径，其余不变：

1. **§5 的返回值**：各档接口从 `run(workspace, budget) -> RunResult` 改为
   `-> AgentRun`。原写法与 §4.3 的数据流自相矛盾——`RunResult.passed` 只能由 harness
   在跑完验证后产出，而 §5 又说它是各档的返回值，等于让执行体自己下判决。
2. **§7.2 的还原目标**：从"基线版本"改为"**test patch 版本**"。实测 `test_auth.py`
   在基线是 140 行、只断言 `/webhook` 与 `/dashboard`，相关断言是修复 commit 新增的；
   还原到基线等于把判据删掉。同时补上"删除 `tests/` 下新增的未跟踪文件"。
3. **§8 的归属**：明确 `fail` 属于判决（`RunResult`），`budget_exhausted` / `error`
   属于自报（`AgentRun`）。§8 原文把三者并列为"三种失败"，没有区分产出者。

另有一处实现期才暴露、原设计未预见的坑，一并记下：**`git archive` 会按
`core.autocrlf` 做导出转换**，在 `autocrlf=true` 的机器上导出的工作区是 CRLF，而
test patch 的上下文是 LF，`git apply` 直接失败。物化时必须显式
`-c core.autocrlf=false -c core.eol=lf`。

### 13.1 评审回合补记（同日晚，code-review 双轴）

§5.0 落盘后跑了一次 Standards + Spec 双轴评审，改掉三处：

1. **还原粒度从 `test_paths` 扩到整棵测试树。** 原实现只 `git checkout` 那几个
   测试文件、只 `git clean` 它们的父目录。实测 `tests/conftest.py` 在基线里就是
   **已跟踪**文件——agent 改它是"修改"而非"新增"，`clean` 与 `checkout` 都碰不到，
   但它能改写整轮收集。现在按测试树根整棵还原（checkout + clean），并在
   `bench/harness/workspace.py` 记下它带来的约束：`test_paths` 必须落在同一个
   专用测试树里。
2. **模型名如实记录。** `AgentRun.model` 原先恒等于 `LLMConfig.model`，fallback
   链接手时会把实际作答的模型写错。现在每轮取 `last_metrics["model_used"]`。
3. **§4.2 / §4.3 的口径对齐。** 树与数据流里残留的 `run(workspace, budget) ->
   RunResult`、"恢复测试文件到基线版本"改成本节 1、2 的写法——原文只有 §5 / §7.2
   / §8 被修正，同一份文档里于是有两个互相矛盾的说法。

### 13.2 §5.1 落地（2026-10-17，专家接工具循环）

§5.1 点名 `collaboration.py` 的专家，但那条链路与实验台的两个前提相冲：专家是
`SubAgent`（没有失败结局），整条图还带 guard / evaluator / HITL / 审计，而 §4.1
要实验台绕开这些。加上 ADR-023 决策 1（原语落 `bench/`、`app/` 零改动），落点定为
**`bench/agent/expert.py`**：角色身份（coder / general / review）接到 §5.0 的
`LLMBackedAgent` 上，工具集、工具契约、停止条件、账本形状全部沿用，产出多一个
`AgentRun.expert` 供多专家档归因。取舍与代价记在 ADR-023 的 §5.1 补记里。

排查结论（§5.1 要求的"先查现有工具循环"）：仓库里三处循环中，
`app/agent_core/react.py` 的 `ReActLoop` 用 JSON-in-text（ADR-023 决策 3 已排除），
`app/agents/stubs.py` 的 `_execute_with_tools` 出口是字符串（没有失败），
`bench/agent/loop.py` 的 `LLMBackedAgent` 才是要复用的那一条。

### 13.3 §5.3 token 主轴（2026-10-18，免费模型倒逼的降级）

09 的真实模型选型（NVIDIA `nemotron-3.5-super-vl-120b-a12b`）是**免费端点**：真实
调用能返回内容与 `tool_calls`，但 `litellm.completion_cost` 抛 `This model isn't
mapped yet`、provider 也不回传成本，`cost_usd` 恒为 0。后果不是"成本很低"，而是
**§5.3 的美元判据根本不触发**——B1 的"等预算"没有约束力，`budget_exhausted` 不会因
钱出现，报告成本列全是 $0。这是在 09 首跑 smoke 时实测出来的（`outcome=fail
status=step_limit cost=$0.000000`）。

按 §5.3 新增的降级路径落地为三处改动：

1. **`BudgetPolicy.axis`**（`bench/harness/budget.py`）：`cost`（默认）/ `tokens`。
   `tokens` 时四档共用同一个 token 上限，美元这条置为 `inf`（永不触发），判停交给
   token；B1/B3 用各自的 token 上限。`main_tokens` / `relaxed_tokens` 是这两个上限；
   `max_tokens` 仍是 cost 主轴下的兜底，语义未变。
2. **报告的 token 列**（`bench/report/aggregate.py`）：每格/每列/每步都多一套 token
   口径（`avg_tokens`、`tokens_per_pass`、`delta_avg_tokens`），B3 多一列增量 token。
   成本列全 0 时报告头部点破"模型无定价信息，请看 token 列"。
3. **B3 的 token 基线**：`RunResult.baseline_tokens` / `incremental_tokens`，由
   `plan_b3_reruns` 从 B1 结果推出，与 `baseline_cost_usd` 并列。

代价：两个主轴并存，读报告时要先看清是哪一轴；`inf` 作为"不设美元上限"的哨兵进了
`Budget`（不进任何落盘 JSON）。真实 token smoke 已复验降级路径有效——`--budget-axis
tokens --main-tokens 20000` 在 25,467 token 处停下并落 `outcome=budget_exhausted`。

### 13.4 L2/L3 的 planner 契约（2026-10-18，真实模型揪出的缺陷）

首轮真实模型跑出四档结局时，L0/L1 是 `budget_exhausted`（66k token 侦察完没动手），
而 L2/L3 各**一次调用、0 步**就结束：`stop_reason="planner 未完成"`。根因是 planner
的接口自相矛盾——`L2PlannerWorkerCritic` 把 `pool.tools` 交给 planner，同时
`PlanPolicy.planner_steps=1`：真实模型第一轮就调工具，唯一的一步被工具轮次吃掉，
`advance` 返回 `step_limit`，整档在分解阶段结束，worker 一次都没跑。离线 scripted
planner 只会回文本、从不调工具，所以此前没暴露。

修法两条（`bench/arms/l2.py`，L3 继承自动生效）：

1. **planner 不暴露工具**（空 `ToolRegistry`）——`chat_with_tools` 带 `tools=[]`，
   模型没有工具通道可调。
2. **planner 用自己的角色指令**（`bench/agent/planner.py::PLANNER_INSTRUCTIONS`）。
   只去掉工具还不够：循环的基础 system prompt 是**编码 agent** 的（"先读再看"），实测
   模型会把工具调用写进正文（`<|open|>argument ... <|call|>`），`parse_plan` 依旧读不
   出来。补上"你是规划者，不探索、不调工具、只回 JSON"后，同一提示直接回出合法数组。

契约因此写成 §5.0 的一个变体：planner 有目标、**没有工具**、只有一轮，产出是文本而非
`AgentRun`——它是四档里唯一不满足"工具 + 循环"的节点，按"分解"这件事本身的性质就该
如此，不是特例。回归用例钉住"planner `tools=[]`、worker 非空"。
