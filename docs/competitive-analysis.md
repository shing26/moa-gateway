# 同类产品对照与取舍

> 调研时间：2026-11-07 · 方法：扒开源仓库 README 与源码（`gh api` 直连），
> 逐产品回答两个问题：**它做对了什么**、**我们为什么不抄 / 已领先什么**。
> 取舍的判据只有一个：本项目的差异化在**治理深度**（审批、留痕、评测、
> 预注册实验），不在功能覆盖面或触发速度。落进边界的决策见 ADR-030/031/036。

## 一、对照表

| 产品 | 形态 | 核心动作 | 我们不抄什么 | 我们已领先什么 |
|---|---|---|---|---|
| [Sweep](https://github.com/sweepai/sweep) | GitHub App | issue 打标签 → 搜索→计划→改码→验证→自审 → 开 PR | 固定五段流水线（我们的链路按 DeliveryRequest 组合，拓扑是实验变量不是常量） | 隔离 worktree + bundle 审批 + 审计链 |
| Copilot coding agent | 云沙箱 | issue 指派给 agent，隔离执行产出 PR | 云端沙箱计费模型 | 本地可复现、零边际成本；trace trailer 归因（ADR-034） |
| Claude Code GitHub Action | 用户 CI | `@claude` 交互 / workflow 自动双模 | 无（双模思路合理，触发侧将来参考 ADR-036 契约 1/4） | 写侧不进 LLM 上下文的凭证隔离 |
| CodeRabbit | SaaS | PR 评审 + "学会团队口味" | 长期数据沉淀（单人项目没有样本量） | 评审结论全部落审计 WAL，可对账 |
| Qodo / PR-Agent | 开源 | `/review` `/improve` 指令式单次调用 | 指令面板（我们的入口是结构化 DeliveryRequest） | 六维离线评测（docs/retrieval-evaluation.md） |
| Renovate / Dependabot | 机器人 | 定时扫描批量开 PR | （触发侧若立项）限流分组按其工业标准做——ADR-036 契约 2/3 的来源 | —— |
| [dsh-session-superagent](https://github.com/MYCF711/dsh-session-superagent) | DSH 会话插件 | 盯仓库 → 唤醒已有会话；凭证不进模型 + 统一 `redact()` | 触发/唤醒层整层——它"故意不做"的交付流水线正是我们的主体（ADR-036） | 输出侧脱敏与本项同构（ADR-035 引为独立趋同佐证）；审计链、预注册实验、等预算剂量实验台 |

## 二、两条从对照中确立的边界

1. **触发层不做**（ADR-036）：所有竞品的自动化都始于"事件自动发起"，也
   都在这里吃过教训（全量轮询、无幂等、无限流）。我们把这层的四个契约
   预注册，触发点满足前不立项。
2. **凭证防线做成纵深**（ADR-035）：输入侧白名单（`subprocess_env`）+
   输出侧落盘/外发统一 `redact()`。dsh-session-superagent 的独立同构实现
   是这条防线的行业共识佐证。

## 三、本页的维护方式

新增竞品或有实证价值的更新时**追加行**，不改已有行的结论；结论变化走
ADR（如 ADR-036 的升级触发点满足时）。本页不替代 ADR——它是输入，ADR
才是决策。
