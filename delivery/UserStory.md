# AICoding 架构设计 · UserStory

> ⚠️ **历史文档（2026-08，未按此实施）**：本文档是 AICoding 文档流水线的产物，用于需求与架构推演。
> 其中**技术选型与评估框架的若干条**（RAGAS / DeepEval / Promptfoo、Prometheus / Grafana 等）
> 在代码里**没有任何引用**——实际落地的是自研 harness（`evals/`）+ 审计 WAL + dashboard。
> 当前口径以 `定位与减法清单.md`、README 与 `docs/adr/` 为准；保留本文档是为了记录推演的来路。

> 本文档为《AICoding 架构设计》核心产物之一，定位为**产品需求与用户故事（UserStory）**。
> 上游输入：《高层架构设计》（G3 已通过，含 §2 痛点 P1~P6、§4 MVP F1~F7 + N1~N5、§5 闭环、§6 功能清单/原型）；《资料摘要》（G1 已通过，根因 A~F 与 文件:行号）；《行业调研报告》（G2 已通过，选型参考）；用户原始诉求 D2。
> 下游输出：驱动《系统设计》（system-architect，G4 并行）的接口契约与模块拆分，以及《部署设计》《安全设计》（本期按用户选择"核心三件套"已跳过）。
> 本文档 Owner：product-story-designer（顾全景，产品故事设计师）。本阶段唯一范围：UserStory、角色场景、验收标准（AC）、非功能需求；不越权替 system-architect 写接口契约/模块拆分细节，不推翻《高层架构设计》已冻结的业务边界（D1~D5、In/Out-of-Scope、U-01/02/03 回退链）。

---

## 0. 元信息：修订记录

```yaml
标题: moa-gateway - UserStory v0.1
版本: v0.1
状态: Reviewing   # Draft | Reviewing | Approved | Deprecated
创建日期: 2026-08-29
最后更新: 2026-08-29
作者: product-story-designer（顾全景，产品故事设计师）
评审人:
  - team-lead（主理人 / 齐构成）
  - system-architect（下游系统设计 Owner，G4 并行）
  - business-architect（上游边界冻结 Owner，G3 已通过）

关联文档:
  上游输入:
    - 高层架构设计: .workbuddy/output/高层架构设计.md（G3 已通过）
    - 资料摘要: .workbuddy/output/material_digest.md（G1 已通过）
    - 行业调研报告: .workbuddy/output/research_report.md（G2 已通过）
    - 用户诉求 D2: 由主理人注入（企业级 Agent 网关，框架已搭、模块未跑通；复用现有代码、避免大规模重写）
  下游产出:
    - 系统设计: AICoding架构设计-2-系统设计.md（G4 并行，接口契约/模块拆分）
    - 部署设计 / 安全设计: 本期按用户选择"核心三件套"已跳过
```

| 版本 | 日期 | 作者 | 变更内容 | 评审状态 |
| --- | --- | --- | --- | --- |
| v0.1 | 2026-08-29 | product-story-designer | 初稿：围绕"把现有框架改造至可运行"拆 6 条 UserStory（US-1~US-6 对应根因 A~F） | Reviewing（待 G4 审核） |

> **版本管理纪律**：破坏性变更（章节结构调整 / 关键故事反转）升 MAJOR；新增故事、扩充 AC 升 MINOR。

---

## 1. 业务背景与价值

### 1.1 业务背景

- **当前业务现状**：moa-gateway 是企业级 Agent 网关，FastAPI 入口、三级意图路由、FSM/HITL 引擎、飞书渠道、守卫、审计等框架层已搭好并能被 `uvicorn app.main:app` 启动（material_digest D1, §7.2）；但业务逻辑未跑通——CoderAgent 永不选中（根因 A）、飞书 HITL 假闭环（根因 B）、检索/知识/RAG 默认全空（根因 C）、两套 schema 割裂（根因 D）、9 个字符串改写脚本致接线漂移（根因 E）、单测 monkeypatch 掩盖真实接线（根因 F）。
- **触发本次需求的事件**：模块逻辑未打通，表现为"框架能起、业务不通"，CI 全绿 ≠ 可运行；用户诉求 D2 要求输出含接口规范/改造步骤/落地优先级/风险提示的完整方案，复用现有代码、避免大规模重写。
- **本系统在产品矩阵中的位置**：企业内网统一 Agent 网关 / 编排入口，上游接飞书 / Webhook / GitHub，下游消费 LiteLLM / Redis / PG / 审计，经意图路由→FSM→Agent→守卫→HITL→审计完成可信交付（见高层 §5.3 业务主链路）。本期 UserStory 仅描述"用户可感知的行为修复"，不定义接口技术细节（留待 system-architect）。

### 1.2 行业方案

> 同类痛点（意图分发错配 / HITL 假闭环 / 伪 RAG / 测试失真）的行业标杆与可借用范式（完整论证见 research_report G2）：

- **意图→Agent 分发键对齐**：借鉴 `semantic-router` 的 `Route.name=路由键` 范式（research_report B5）——路由结果名直接作为下游 Agent 分发键，0 LLM 调用、毫秒级；并吸收 `Rasa` 的"intent 名必须与注册 action 名严格一致"契约思想（research_report B4）。本期落地为**自研对齐映射表**（不引库，先止血，见高层 D3），不引入 semantic-router 运行时。
- **HITL 真闭环**：借鉴 `Dify` Human Input 节点的"多分支审批 + 超时升级 + 评论回投"、`LangGraph` 的 `interrupt()/Command(resume)` 状态翻转、`n8n` 的 Wait-on-webhook + 超时升级 + 审计（research_report B1/B2/B3）。本期落地为"卡片 action 回调指向 `/webhook/callback` → `engine.handle_event` 翻转状态 → 回投 agent_output"。
- **真向量 RAG**：借鉴 `Dify` 混合检索（向量+全文+重排）质量最佳实践（research_report B1），本期落地为 pgvector + 向量/关键词混合、离线关键词回退（轻量版，F3）。
- **评估反制测试失真**：借鉴 `RAGAS / DeepEval / Promptfoo` 标准评估框架接入 CI 门禁（research_report S6/F），去除 monkeypatch 真实集成测试（F5）。

### 1.3 方案收益与价值

| 编号 | 价值维度 | 量化目标 | 当前值 | 目标值 | 对齐痛点 |
| --- | --- | --- | --- | --- | --- |
| V1 | 意图分发正确率 | 意图→Agent 注册键对齐率 | 0%（CoderAgent 永不选中，根因 A） | 100% | P1（A） |
| V2 | 飞书 HITL 闭环率 | 审批结果真正回投率（非仅回执） | 0%（仅回执，根因 B） | 100% | P1（B） |
| V3 | 测试真实化 | 真实接线集成测试占比（去 monkeypatch） | 低（大量 monkeypatch，根因 F） | ≥ 80% | P1（F） |
| V4 | 检索召回可用 | 混合检索命中率（真向量 RAG 轻量版） | 0%（retrieve 永远空，根因 C） | ≥ 基线（向量+关键词） | P2（C） |
| V5 | 存储统一 | schema 割裂数 | 2 套（code_review_prs / code_review_vectors 互不消费，根因 D） | 1 套（单 PG 承载） | P2（D） |
| V6 | 脚本清零 | 字符串改写源码脚本数 | 9（gen_main 等，根因 E） | 0（停用 + 接线回归） | P3（E） |

> 量化标准：每条均对齐高层 §2.3 期待目标，无"提升/优化/加强"等模糊词；V1/V2/V3 为 P1 合规/业务底线，MVP 上线即须达 100% / ≥80%。

### 1.4 术语清单

| 术语 | 英文 / 缩写 | 含义 |
| --- | --- | --- |
| 意图路由 | intent router | 三级降级（正则→微模型→路由 LLM）输出意图标签的模块（app/router/intent_router.py） |
| Agent 注册键 | agent key | agents/loader.py 注册表中的键（coder / general / review），取 Agent 的依据 |
| 键对齐映射 | intent→agent map | 消除根因 A 的自研映射表，使意图标签集合与注册键集合同构 |
| HITL | Human-in-the-Loop | 人在环审批；飞书卡片批准/拒绝后需真正闭环回投 |
| 假闭环 | fake loop | 根因 B：卡片 action 仅回执"已批准"，请求仍挂起、结果不送达 |
| FSM | Finite State Machine | 有限状态机编排引擎（app/engine.py），承载 HITL 挂起/恢复 |
| monkeypatch | — | pytest 单测中替换真实函数/对象以制造"通过"的掩盖手段（根因 F） |
| pgvector | — | Postgres 向量扩展，承载关系+向量统一存储（F4） |
| Golden Dataset | — | 评估门禁使用的标注数据集（≥50 例，F5） |
| 回退链 | fallback chain | 高层 §4.3 冻结的环境回退（U-01/02/03），非待确认分歧 |

---

## 2. 范围与边界

### 2.1 系统内模块及功能

> 对齐高层 §6.1 In-Scope 与 §6.2 模块全景，本系统（改造后）须跑通的能力：

| 一级模块 | 二级功能 | 功能项 | 优先级 | MVP 是否包含 | 说明（对齐功能编号） |
| --- | --- | --- | --- | --- | --- |
| 接入层 | 飞书渠道 | 消息收发 / 卡片 action 回调 / 验签 | P0 | 是 | F2 真闭环端点接收入口 |
| 接入层 | Webhook 渠道 | 外部系统 / GitHub 事件接入 | P0 | 是 | 复用现有路由 |
| 意图路由 | 注册键对齐 | 自研意图→Agent 键对齐映射表 | P0 | 是 | F1，消除根因 A |
| 业务能力层 | FSM 编排 | 状态转移 / HITL 挂起恢复 | P0 | 是 | 复用 + 修正接线 |
| 业务能力层 | Agent 执行 | Coder / General / Review 正确分发 | P0 | 是 | F1 |
| 业务能力层 | 守卫 | 内网 IP / 密钥 / RBAC / 红队 | P0 | 是 | N2 审计联动 |
| 业务能力层 | HITL 闭环 | 状态翻转 + 结果回投 | P0 | 是 | F2，消除根因 B |
| 检索 / 知识 | 轻量 RAG | pgvector + 向量/关键词混合，离线回退 | P1 | 是（轻量） | F3，消除根因 C |
| 存储 | 统一 PG | 单 PG+pgvector 合并两套 schema | P0 | 是 | F4，消除根因 D |
| 评估 | CI 门禁 | RAGAS/DeepEval/Promptfoo，去 monkeypatch | P0 | 是 | F5，反制根因 F |
| 治理 | 接线回归 | 停用字符串改写脚本，回归正确骨架 | P0 | 是 | F6，消除根因 E |
| 基础能力层 | LiteLLM | 多模型代理 / fallback / cost | P0 | 是 | 复用（D2） |
| 基础能力层 | Redis | 状态栈 / Lua 锁 | P0 | 是 | 复用 |
| 基础能力层 | 审计 WAL+ES | 关键操作 100% 留痕 | P0 | 是 | N2 |
| 部署 | 单租户内网 | 私有化企业内网 | P0 | 是 | N1 |

### 2.2 系统外模块及功能

> 当前系统**不覆盖**的功能（对齐高层 §6.1 Out-of-Scope O1~O4），及其原因：

| 编号 | 不做的事 | 原因 | 后续计划 | 是否影响本 US |
| --- | --- | --- | --- | --- |
| O1 | 多 Agent 复杂编排 runtime（supervisor 多级） | MVP 先跑通分发与闭环，多级协作为增强项（D1） | 完整版（R6） | 否，本期仅 Coder/General/Review 三 Agent 分发 |
| O2 | Dify / n8n / Rasa 平台级替换 | 违背用户冻结的"复用现有代码、避免大规模重写"约束（D2 §4） | 不做（否决） | 否，仅吸收范式不引运行时 |
| O3 | 大规模重写（推倒自研骨架） | 框架已搭、仅业务逻辑未通；重写风险高（D1/D3） | 不做（仅修正接线） | 否，本 US 均为最小接线修正 |
| O4 | 跨云多租户 | 本期定位企业内网单租户（N1） | 后续版本按需评估 | 否，单租户已覆盖 |

### 2.3 外部依赖

| 依赖系统 | 提供方 | 依赖能力 | 接入方式 | 接口人 |
| --- | --- | --- | --- | --- |
| 飞书开放平台 | 飞书 | 消息收发 / 卡片 action 回调 / 验签 | HTTPS Webhook（验签），卡片 action 回调必须指向 /webhook/callback | 飞书管理员（U-03 配置权限） |
| GitHub | GitHub | PR 事件 / 代码获取 | HTTPS Webhook / REST | 仓库管理员（GITHUB_TOKEN） |
| LiteLLM 上游模型 Provider | 模型厂商 / 自研代理 | 多模型推理 / fallback / cost 计量 | HTTPS（LiteLLM 代理，进程内 SDK） | 平台研发 |
| Postgres + pgvector | 企业内网 PG 实例 | 关系数据 + 向量（合并两套 schema） | psycopg（裸 SQL） | DBA（U-02 实例有无） |
| 评估框架 RAGAS / DeepEval / Promptfoo | 开源引入 | 意图准确率 / faithfulness / 红队 | CI 调用（进程内 + CLI） | 质量研发 |
| Redis | 企业内网 | HITL 挂起态 / 状态栈 / Lua 锁 | Redis 协议（无 REDIS_URL 则内存回退） | 平台研发 |
| 审计 WAL + ES | 自研 | 关键操作留痕 | 文件 WAL + 可选 ES 双写 | 安全研发 |

> 所有 P1 痛点对应核心能力均有依赖来源：A→意图路由自研映射（无外部依赖）；B→飞书回调端点（外部=飞书）；F→评估框架 CI 门禁（外部=评估框架）。环境不确定项（U-01 embedding 外联 / U-02 PG 实例 / U-03 飞书回调权限）已在高层 §4.3 冻结回退链，不作静默选择。

---

## 3. 功能清单

### 3.1 功能清单结构

> 结构同高层 §6.3（一级模块 / 二级模块 / 功能项 / 优先级 / MVP 范围 / 完整版范围 / 备注），复用高层功能清单并映射至根因与 UserStory：

| 一级模块 | 二级模块 | 功能项 | 优先级 | MVP 范围 | 完整版范围 | 备注（对齐根因 / US） |
| --- | --- | --- | --- | --- | --- | --- |
| 意图路由 | 注册键对齐 | 建立意图标签→Agent 注册键同构映射表，Coder/Review 可命中 | P0 | 是 | 是 | 根因 A / US-1 |
| HITL | 飞书真闭环 | 卡片 action 回调指向 /webhook/callback，状态翻转 + 回投 agent_output | P0 | 是 | 是 | 根因 B / US-2 |
| 检索/知识 | 轻量 RAG | pgvector + 向量/关键词混合检索，离线关键词回退 | P1 | 是（轻量） | 是（重排/多库） | 根因 C / US-3 |
| 存储 | 统一 PG | 单一 PG+pgvector 承载关系+向量，合并两套 schema | P0 | 是 | 是 | 根因 D / US-4 |
| 评估 | CI 门禁 | RAGAS/DeepEval/Promptfoo 接 CI，去 monkeypatch 真实集成测试 | P0 | 是 | 是 | 根因 F / US-5 |
| 治理 | 接线回归 | 停用 gen_main 等字符串改写脚本，接线回归正确骨架 | P0 | 是 | 是 | 根因 E / US-6 |
| 接口规范 | 调用设计 | 冻结下游 G4 接口边界与依赖声明 | P0 | 是 | 是 | V1/V2（下游 system-architect） |
| 部署 | 单租户内网 | 私有化企业内网、单租户隔离 | P0 | 是 | 是 | N1 |
| 合规 | 审计留痕 | 关键操作 100% 写入审计 WAL | P0 | 是 | 是 | N2 |
| 质量 | 测试真实化 | 真实接线集成测试占比 ≥ 80% | P0 | 是 | 是 | V3 / N3 |
| 约束 | 复用不重写 | 仅吸收范式 + 修正接线，不引运行时平台 | P0 | 是 | 是 | N4 |
| 体验 | HITL 闭环率 | 飞书审批结果回投率 100% | P0 | 是 | 是 | V2 / N5 |
| 协作 | 多级 supervisor | 多 Agent 复杂编排（延后） | P2 | 否 | 是 | R6（O1） |

> 硬指标自检：每个 P0 功能（F1~F7、N1~N5）MVP 范围均为"是"；每个功能均映射至 §2.5 缺口与根因 A~F 及对应 US 故事。

---

## 4. 角色与场景

### 4.1 角色清单

| 角色 | 业务身份 | 主要操作 | 核心关注点 |
| --- | --- | --- | --- |
| 内部开发者 | 一线使用者 | 经飞书 / Webhook 提交 Agent 请求（写码、翻译、搜索、Review） | 请求被正确路由到对应 Agent、结果可靠可复现 |
| 运维 / SRE | 管理人员 | 监控 / 干预 / 排障 / 发布 / 看板核对 | 系统可观测、HITL 可干预可恢复、故障可定位 |
| 终端用户（被服务方） | 业务方 / C 端 | 经飞书触达网关获取答复 / 审批结果 | 审批 / 答复及时送达（非仅"已批准"回执） |
| 合规 / 安全 | 合规 / 安全研发 | 审计复核 / 越权拦截复核 / 红队 | 关键操作 100% 留痕、越权与红队可控 |
| 甲方决策者（CTO） | 业务负责人 | 看板审阅 / ROI 决策 / 上线审批 | 投入产出比明确、上线风险可控、避免大规模重写 |

> 对齐高层 §2.1 角色；本期用户故事视角以"内部开发者 / 终端用户"为主，运维 / 合规为闭环守护方。

### 4.2 关键场景清单

| 编号 | 角色 | 触发条件 | 期望结果 | 频率（日均 / QPS） |
| --- | --- | --- | --- | --- |
| S-01 | 内部开发者 | 经飞书提交 "写一段快排代码" / "review 这段 PR" | 请求分发至 Coder / Review Agent，专用 system prompt 生效 | 高频（数十~数百次/日） |
| S-02 | 终端用户 | 飞书卡片点击"批准"代码审查结论 | 审批状态翻转，agent_output 真正回投到发起方会话 | 中频（与 PR 审查量相关） |
| S-03 | 内部开发者 | 提交需检索知识库的请求（如"参考团队规范改写"） | retrieve() 返回非空，混合检索召回相关片段 | 中频 |
| S-04 | 运维 / SRE | HITL 请求挂起超过 24h 未审批 | 超时升级分支触发（告警 / 转人工 / 转默认策略） | 低频（异常态） |
| S-05 | 合规 / 安全 | 任意关键操作（路由 / 守卫 / HITL 翻转）发生 | 审计 WAL 100% 留痕，可回放 | 全量（随主链路） |
| S-06 | 质量研发 | CI 流水线执行评估门禁 | 真实集成测试 ≥ 80% 占比，门禁失败阻断合入 | 每次提交 / 每日定时 |

---

## 5. 用户旅程（UserStory）

> 每条 UserStory 均按 5.x.1~5.x.7 七节展开（业务场景 / 业务流程 / UE 原型 / 业务逻辑 / 数据描述 / 验收标准 / 外部集成接口）。US-1~US-6 分别对应根因 A~F 的修复场景，全部在高层已冻结边界内细化，不推翻任何已冻结决策。

### 5.1 US-1 意图正确分发（消除根因 A）

#### 5.1.1 业务场景

- **视角**：内部开发者
- **描述逻辑**：内部开发者经飞书或 Webhook 提交一段编程 / 翻译 / 搜索 / 代码审查请求时，系统应能依据意图标签命中正确的 Agent 注册键（coder / general / review），使对应 Agent 的专用 system prompt 真正生效，而不是全部塌缩到 GeneralAgent。

#### 5.1.2 业务流程

- **视角**：用户
- **描述方式（Given / When / Then）**：
  - Given 开发者在飞书发送"帮我用 Python 写一段快速排序"，When 意图路由输出 `coding` 并经键对齐映射得到 `coder`，Then 请求被分发至 CoderAgent 且专用 system prompt 生效、返回代码实现。
  - Given 开发者发送"review 一下这段 PR diff"，When 意图命中 `review` 映射键，Then 请求分发至 ReviewAgent。
  - Given 开发者发送无法归类的闲聊，When 无任何映射命中，Then 回退至 `general` 并给出通用回复（不报错）。

#### 5.1.3 UE 原型

- 对话 / 请求页：在答复区上方显示"分发：Coder / Review / General"标签与命中意图，便于开发者确认请求被正确理解（对齐高层 §6.4 对话请求页）。

#### 5.1.4 业务逻辑

- **视角**：业务系统
- **描述方式（时序）**：
  1. `routes/feishu.py` 或 `routes/webhook.py` 收到请求 → `pipeline.run()`。
  2. `engine.handle_event()`（FSM）→ `router.route(text)` 输出意图标签（如 `coding`）。
  3. 新增 `INTENT_TO_AGENT` 映射表：将 `coding→coder`、`translate→general`、`search→general`、`analyze→general`、`summarize→general`、`review→review`、`greeting→general`、`control→general`、`debug→general`、`assistant→general` 同构对齐。
  4. `get_agent(intent)` 改为查映射后的注册键（`agents/loader.py:4-11` 注册表仅含 coder/general/review），命中即返回对应 Agent；无映射则回退 `general`。
  5. `agent.execute(envelope)` 执行，`command_mode.get()` 强制意图覆盖仍优先（如 `/review` 命令）。

#### 5.1.5 数据描述

- 核心数据流：`text → intent_label → mapped_key → Agent 实例`。
- 映射表作为配置（`prompt_registry` 或独立 `intent_map.py`）持久化，启动期自检"意图集合 ⊆ 注册键集合 ∪ {general}"，避免再次出现根因 A 的键不匹配。

#### 5.1.6 验收标准 AC

- **正常路径**：
  - Given 开发者提交编程类请求，When 键对齐映射命中 coder，Then CoderAgent 被选中且其 system prompt 生效，返回内容区别于 GeneralAgent 通用回复。
  - Given 开发者提交 /review 命令，When 意图映射命中 review，Then ReviewAgent 被选中。
- **异常路径（未知意图回退 general）**：
  - Given 开发者提交无法匹配任何已知意图的语句，When 映射表无对应键，Then 请求安全回退至 `general` 且不抛异常、不挂起。
- **异常路径（LLM 降级失败回退）**：
  - Given 三级路由中微模型 / 路由 LLM 调用失败或超时，When 降级到正则兜底仍无法判定，Then 仍按映射表回退 `general` 并保证主链路不中断（复用 material_digest D1, §7.3 降级行为）。
- **防回归**：集成测试断言 `get_agent("coding")` 不再返回 None（反制 material_digest 根因 F 中 test_pipeline.py:148 的 monkeypatch 绕过）。

#### 5.1.7 外部集成接口

- 无外部系统依赖；纯内部注册表 + 映射表修正。涉及下游 system-architect 在 G4 冻结"意图路由→Agent 取用"的调用契约（F7）。

### 5.2 US-2 飞书 HITL 真闭环（消除根因 B）

#### 5.2.1 业务场景

- **视角**：终端用户 / 内部开发者
- **描述逻辑**：当请求进入 REVIEW 需人工审批时，飞书卡片的"批准/拒绝"动作应真正驱动状态翻转并将 agent_output 回投到发起方会话；用户点批准后应收到**真实结论**，而非仅"已批准"回执。

#### 5.2.2 业务流程

- **视角**：用户
- **描述方式（Given / When / Then）**：
  - Given 审批人点击飞书卡片"批准"，When 卡片 action 回调 POST 到 `/webhook/callback` 并携带 `HUMAN_APPROVED`，Then `engine.handle_event` 翻转 HITL 状态、`remove_hitl`、经 `adapter.adapt(hitl.agent_output)` 将结果回投到原会话。
  - Given 审批人点击"拒绝"，When 回调携带 `HUMAN_REJECTED`，Then 状态翻转并回投拒绝原因，请求关闭。

#### 5.2.3 UE 原型

- 审批卡片（飞书）：含"批准 / 拒绝 / 评论"三 action（借鉴 Dify 多分支 + 评论回投）；审批结果回执页展示 agent_output 真正送达（对齐高层 §6.4 审批卡片 + 审批结果回执）。

#### 5.2.4 业务逻辑

- **视角**：业务系统
- **描述方式（时序）**：
  1. `routes/feishu.py:69-96` 的 `card_action` 分支须改为 POST 至 `/webhook/callback`（而非仅 `adp.send()` 回执）。
  2. `routes/webhook.py:16-65` 的 `/webhook/callback` 已含 `engine.handle_event(HUMAN_APPROVED/REJECTED)` → `adapter.adapt(hitl.agent_output)`（真闭环点）。
  3. `engine` 翻转 FSM 状态、`remove_hitl` 清除挂起、调用 `card_sender` / `adapter` 将 agent_output 回投到 `feishu_chat_id`。
  4. 审计 WAL 写入"HITL 翻转"事件（N2）。
  5. 若飞书凭证缺失（`deps.py:107` `card_sender=None`），REVIEW 仅挂起不发卡，回投降级为飞书机器人私聊或轮询（高层 U-03 回退，闭环端点不变）。

#### 5.2.5 数据描述

- 核心数据：`hitl_request{id, status, agent_output, feishu_chat_id, approved_by, ts}`；状态从 `PENDING` → `APPROVED/REJECTED` → `DELIVERED`。
- 回投完成后 `DELIVERED` 状态与 agent_output 一并写入审计 WAL，供回放。

#### 5.2.6 验收标准 AC

- **正常路径（真正送达）**：
  - Given 审批人点击"批准"，When 回调到达 `/webhook/callback` 且 `handle_event` 翻转状态，Then 发起方飞书会话收到**真实的 agent_output 结论内容**（代码审查意见 / 生成结果），而非仅"已批准"回执文本。
  - Given 审批人点击"拒绝"，When 回调到达，Then 发起方收到拒绝原因且请求关闭。
- **异常路径（结果送达失败）**：
  - Given 状态已翻转但回投通道（飞书/adapter）异常，When 回投失败，Then 请求标记为 `DELIVERY_FAILED` 并触发重试 / 告警，不静默丢弃（防根因 B 重现）。
- **超时升级分支**：
  - Given HITL 请求挂起超过 24h（默认阈值）未审批，When 超时计时器触发，Then 进入升级分支：发送超时提醒给审批人 + 转交备用审批人 / 值班 SRE，并在看板标记（对齐高层 §6.4 超时升级 + n8n Wait-on-webhook 范式）。
- **防回归**：集成测试断言"卡片 action 回调后原会话收到 agent_output"，禁止仅凭 `adp.send()` 回执即判定闭环（反制 research_report R-02）。

#### 5.2.7 外部集成接口

- 飞书开放平台：卡片 action 回调 URL 配置为 `/webhook/callback`（高层 U-03）；需飞书管理员配置 Message Card Request URL 权限。验签沿用 `app/channels/feishu*.py` 现有逻辑。

### 5.3 US-3 真向量 RAG 召回（消除根因 C）

#### 5.3.1 业务场景

- **视角**：内部开发者
- **描述逻辑**：当请求需要知识库支撑（如"参考团队编码规范改写"），`retrieve()` 应返回非空的相关片段，而非永远空（material_digest 根因 C：`pipeline.py:179` 永远返回空）。

#### 5.3.2 业务流程

- **视角**：用户
- **描述方式（Given / When / Then）**：
  - Given 开发者提交需检索的请求，When `retriever.retrieve(query)` 执行，Then 返回 Top-K 相关片段（向量召回 + 关键词召回混合），非空且带来源引用。
  - Given 离线 / embedding 不可用，When 向量召回失败，Then 回退关键词检索仍返回可用片段。

#### 5.3.3 UE 原型

- 知识检索浮层（完整版增强，MVP 可仅日志/审计溯源）：展示召回来源与相似度（对齐高层 §6.4 知识检索浮层）。

#### 5.3.4 业务逻辑

- **视角**：业务系统
- **描述方式（时序）**：
  1. 将 `deps.py:35` 的 `ContextRetriever(VectorDBClient())` 内存 dict 替换为 pgvector 客户端（F4 同一 PG 实例）。
  2. `retrieve()` 实现混合检索：向量召回（pgvector 余弦）+ 关键词召回（中文 bigram / 全文），合并重排取 Top-K。
  3. `knowledge.py` 由内存改为读取 PG 知识表；Obsidian 同步在 `OBSIDIAN_VAULT_PATH` 配置时启用。
  4. 嵌入：默认外部 embedding API；禁外联时回退本地 FastEmbed / bge（高层 U-01）。

#### 5.3.5 数据描述

- 核心数据：`documents{id, content, embedding, source, updated_at}`（pgvector 列）；查询向量与 `content` 同库混合召回。
- 召回结果写入上下文并进入审计（N2）。

#### 5.3.6 验收标准 AC

- **正常路径**：
  - Given 知识库已摄入至少 N 条文档，When 提交相关查询，Then `retrieve()` 返回非空列表且 Top-1 相似度 ≥ 阈值，结果被用于 agent 上下文。
- **异常路径（离线关键词回退）**：
  - Given 向量检索因 embedding 故障不可用，When 触发回退，Then 关键词检索仍返回非空可用片段，主链路不中断（对齐高层 U-01）。
- **异常路径（知识库为空）**：
  - Given 知识库无任何文档，When 检索，Then 返回空并在上下文中标注"无知识库支撑"，不报错、不伪造召回。

#### 5.3.7 外部集成接口

- embedding Provider（外部 API 或本地模型，U-01）；Postgres+pgvector（F4，见 §5.4）。评估框架对召回质量打分（F5）。

### 5.4 US-4 统一存储合并 schema（消除根因 D）

#### 5.4.1 业务场景

- **视角**：运维 / SRE
- **描述逻辑**：消除 `code_review_prs`（PG+pgvector）与 `code_review_vectors`（SQLite）两套割裂 schema；统一由单一 PG 实例承载关系 + 向量，旧 SQLite 不再被孤立。

#### 5.4.2 业务流程

- **视角**：用户
- **描述方式（Given / When / Then）**：
  - Given 系统启动并连接统一 PG，When PR 审查写入与 RAG 向量写入发生，Then 两者均落同一 PG 实例的合并 schema，无跨库孤岛。
  - Given 迁移脚本执行完毕，When 运行时访问旧 SQLite，Then 旧 SQLite 不再被任何代码路径读写（被合并表取代）。

#### 5.4.3 UE 原型

- 运维看板展示存储实例数 = 1（关系+向量同库），无孤立 SQLite 连接（对齐高层 §6.5 运行看板）。

#### 5.4.4 业务逻辑

- **视角**：业务系统
- **描述方式（时序）**：
  1. 合并 `apps/code_review_pipeline/storage/schema.sql`（建 `code_review_prs` 含 pgvector 列）与 `rag/vector_store.py:104-129` 的向量结构为单表 / 同库两表，统一由 `CODE_REVIEW_DATABASE_URL` 驱动的 psycopg 访问。
  2. `review_store.build_review_store()` 与 RAG 向量库共用同一 PG 连接与 schema 管理（`_ensure_schema()` 幂等建表）。
  3. 删除 / 停用 `data/code_review_vectors.sqlite` 的读写代码路径；本地开发回退用 SQLite 仅作无 PG 时的等价回退（高层 U-02）。
  4. 编写迁移脚本：从旧 SQLite 导入向量至 PG（若历史数据需保留）。

#### 5.4.5 数据描述

- 核心数据：`code_review_prs(id, repo, diff, finding_jsonb, embedding vector)` 与 RAG 文档表同库；单一连接池。
- 旧 SQLite 文件路径从配置与代码中移除（或仅标记为回退）。

#### 5.4.6 验收标准 AC

- **正常路径**：
  - Given PG 实例可用，When PR 审查与 RAG 向量写入，Then 全部落同一 PG 实例，审查记录与向量可被同一查询关联。
- **异常路径（迁移后旧 SQLite 不再被孤立）**：
  - Given 迁移脚本执行完成，When 运行时启动并执行全量集成测试，Then 代码中无任何活跃路径读写 `data/code_review_vectors.sqlite`（旧 SQLite 不被孤立引用），断言其未被打开。
- **异常路径（无 PG 回退）**：
  - Given 未配置 `CODE_REVIEW_DATABASE_URL`，When 启动，Then 回退 SQLite 等价承载（高层 U-02），且明确日志提示"回退模式"，不崩溃。

#### 5.4.7 外部集成接口

- Postgres + pgvector（企业内网，U-02）；DBA 提供实例与迁移授权。

### 5.5 US-5 评估 CI 门禁真实化（反制根因 F）

#### 5.5.1 业务场景

- **视角**：质量研发 / 运维
- **描述逻辑**：去除单测中掩盖真实接线的 monkeypatch（material_digest 根因 F：`test_pipeline.py:148` 把 `get_agent` 替换成恒返回假 Agent），使 CI 绿真正代表业务通；真实接线集成测试占比 ≥ 80%，门禁失败阻断合入。

#### 5.5.2 业务流程

- **视角**：用户
- **描述方式（Given / When / Then）**：
  - Given 开发者提交 PR，When CI 运行评估门禁，Then 真实 `get_agent` / 真实 `/webhook/callback` 集成测试执行，意图准确率 / faithfulness / 红队指标计入。
  - Given 评估门禁任一指标低于阈值，When 门禁判定失败，Then 合入被阻断并输出失败归因。

#### 5.5.3 UE 原型

- 评估门禁面板（CI 联动）：展示意图准确率 / faithfulness / 红队趋势，门禁失败标红（对齐高层 §6.5 评估门禁面板）。

#### 5.5.4 业务逻辑

- **视角**：业务系统
- **描述方式（时序）**：
  1. 重写 `tests/unit/test_pipeline.py` 等：移除 `monkeypatch.setattr(pipeline_module,"get_agent",...)` 类掩盖，改为真实注册表 + 真实映射（US-1）驱动。
  2. 新增集成测试：真实飞书 `/webhook/callback` 闭环（US-2）、真实 pgvector 检索（US-3）、真实 PG 合并 schema（US-4）。
  3. 引入 RAGAS（faithfulness/答案相关性）、DeepEval（pytest 门禁）、Promptfoo（红队 / 提示注入）接 CI。
  4. Golden Dataset ≥ 50 例（intent/guard/e2e），CI 门禁设阈值。
  5. 占比统计：集成测试中"真实接线"用例数 / 总关键路径用例 ≥ 80%。

#### 5.5.5 数据描述

- 核心数据：评估结果为结构化的 `{metric, score, threshold, pass}`；门禁状态写 CI 报告与审计（N2）。

#### 5.5.6 验收标准 AC

- **正常路径**：
  - Given 关键路径改动，When CI 跑评估门禁且真实集成测试占比 ≥ 80%，Then 所有指标达阈值则合入放行。
- **异常路径（门禁失败阻断合入）**：
  - Given 某指标低于阈值（如意图准确率低于 100% / faithfulness 低于基线），When 门禁判定，Then 合入被阻断并输出失败用例与归因，禁止"CI 绿但业务断"。
- **异常路径（monkeypatch 禁止）**：
  - Given 测试代码中仍存在 `monkeypatch` 替换 `get_agent` / `engine.handle_event` 等真实接线点，When 静态检查 / 测试清单扫描，Then 该测试被标记为"非真实"并从真实占比中剔除，触发整改。

#### 5.5.7 外部集成接口

- 评估框架 RAGAS / DeepEval / Promptfoo（CI 调用）；LLM-as-judge 裁判模型（复用 LiteLLM，注意 R-04 成本/偏见缓解）。

### 5.6 US-6 停用字符串改写脚本接线回归（消除根因 E）

#### 5.6.1 业务场景

- **视角**：运维 / SRE
- **描述逻辑**：停用 `gen_main.py` / `patch_main.py` / `fix_main_all.py` 等 9 个用字符串 `.replace()` 直接改写 `app/main.py` 的脚本（material_digest 根因 E），使生产代码接线回归正确骨架，杜绝无类型检查的文本 patch 造成 A/B 断裂。

#### 5.6.2 业务流程

- **视角**：用户
- **描述方式（Given / When / Then）**：
  - Given 仓库清理后启动，When 构建 / 启动 `uvicorn app.main:app`，Then 入口装配来自正确骨架（`deps.py` 单例、`routes` 挂载、HITL 块保留），无脚本二次改写。
  - Given CI 流水线执行，When 静态检查扫描 `scripts/`，Then 字符串改写源码类脚本被禁用 / 删除，CI 仍可绿。

#### 5.6.3 UE 原型

- 运维看板"治理"指标：字符串改写脚本数 = 0（对齐 V6）；CI 全绿且真实链路通（对齐 V3）。

#### 5.6.4 业务逻辑

- **视角**：业务系统
- **描述方式（时序）**：
  1. 在 `scripts/` 中删除或隔离 9 个字符串改写脚本（`gen_main.py:4-27`、`patch_main.py`、`fix_main_all.py`、`fix_hitl.py`、`fix_name.py` 等），其历史意图（loader import、exception handler、HITL 块、agent_name 解析、review state 字符串）固化进 `app/main.py` / `deps.py` 正确骨架。
  2. 将"HITL 块 / review state 解析"等原被脚本删除/改写的逻辑，以正式代码回归 `app/engine.py` 与 `pipeline.py`。
  3. 增加 CI 守卫：禁止 `scripts/` 中出现 `open("app/main.py").read().replace(...)` 类模式（或标记为废弃）。

#### 5.6.5 数据描述

- 核心数据：无新增数据表；变更集中在代码装配与 `scripts/` 清单。审计记录"治理：停用字符串改写脚本"事件（N2）。

#### 5.6.6 验收标准 AC

- **正常路径**：
  - Given 仓库已移除字符串改写脚本，When `uvicorn app.main:app` 启动，Then 入口装配正确（HITL 块存在、review state 解析正确、CoderAgent 可命中），无需任何脚本后处理。
- **异常路径（CI 仍可绿且真实链路通）**：
  - Given 停用脚本后运行完整 CI，When 评估门禁 + 真实集成测试执行，Then CI 仍可绿，且真实链路（US-1~US-5）全部跑通，证明接线已回归正确骨架。
- **异常路径（防回潮）**：
  - Given 有 MR 重新引入字符串改写源码脚本，When CI 静态扫描命中 `.replace(...)` 改写 `app/*.py`，Then 该 MR 被阻断并要求改为正式代码。

#### 5.6.7 外部集成接口

- 无外部系统依赖；CI 流水线（GitHub Actions / 内部 CI）执行静态扫描与门禁。

---

## 6. 非功能性需求

### 6.1 易用性需求

- **操作便利性**：开发者经飞书 / Webhook 提交请求无需学习新界面；答复区展示"分发：Agent 类型"标签，降低"请求被错误路由"的认知成本。
- **引导与反馈**：HITL 卡片 action 命名清晰（批准 / 拒绝 / 评论）；审批结果回执明确展示真实结论而非回执文本（对齐 US-2 AC）。
- **错误反馈**：未知意图回退 general 时给出"已转通用处理"提示；检索为空时明确"无知识库支撑"（对齐 US-1/US-3 异常路径）。
- **一致性**：飞书卡片、回执、看板三类触点术语一致（Coder/Review/General、HITL、闭环）。
- **无障碍**：飞书卡片文本可读、动作按钮可达；不依赖颜色单一通道传达状态。

### 6.2 性能响应需求

- **审批闭环时延**：用户点击批准到 agent_output 回投的端到端 P99 ≤ 5 秒（对齐高层 §6.4 关键交互约束 + N5/V2）；P50 ≤ 1 秒。
- **路由时延**：意图路由（三级降级 + 键对齐）P99 ≤ 500 毫秒，正则层 P99 ≤ 50 毫秒（避免阻塞主链路）。
- **检索时延**：混合检索（向量 + 关键词）Top-K（K=5）P99 ≤ 800 毫秒；离线关键词回退 P99 ≤ 300 毫秒。
- **吞吐量**：单租户企业内网场景，主链路峰值 ≥ 20 QPS 可持续；HITL 并发挂起 ≥ 100 不受影响。
- **数据规模**：PG 实例关系 + 向量统一承载，向量规模 MVP ≤ 百万级（pgvector 性价比区间，research_report SR-09/10）；超 500 万再评估 Qdrant（完整版）。

### 6.3 操作与环境需求

- **部署形态**：私有化企业内网、单租户（N1）；不跨云、不出域。
- **运行环境**：Python ≥ 3.12（与 `pyproject.toml:4` 一致）；生产以 `uvicorn app.main:app` 启动；Docker 以 `python:3.12-slim` 为基础。
- **网络环境**：企业内网可达飞书回调、GitHub Webhook、PG、Redis、LiteLLM 代理；无外联时 embedding 走本地 FastEmbed（U-01）。
- **兼容性**：飞书客户端（移动 / 桌面）；Webhook 调用方为标准 HTTPS 客户端；无需独立前端（本期以飞书卡片 + 内部看板为触点）。
- **降级环境**：无 REDIS_URL 时内存回退、无 LLM key 时优雅降级（material_digest D1, §7.3）；无 PG 时 SQLite 等价回退（U-02）。

### 6.4 安全性需求

#### 6.4.1 安全密码设置

- 本系统不提供终端用户自助注册 / 密码登录；鉴权经飞书验签 + Webhook Token（见 6.4.2 / 6.4.3）。若后续运营后台引入口令登录，须满足 **8 位以上大小写字母 + 数字 + 特殊字符**。

#### 6.4.2 安全软件架构

- **通信安全**：飞书回调、Webhook、GitHub 事件均经 HTTPS；回调须验签（飞书 verification token / GitHub 签名）。
- **认证与访问控制**：Webhook / Dashboard 需 `WEBHOOK_AUTH_TOKEN` / `DASHBOARD_PASSWORD`；飞书需 `FEISHU_VERIFICATION_TOKEN`。
- **接口安全**：限制未经许可的接口访问；外部系统仅能触达声明端点（/feishu/event、/webhook/{channel}、/webhook/callback）；使用安全通讯协议。
- **鉴权 fail-open 修复（关键）**：当前 `app/middleware/auth.py:43,46` 在 token 为空时直接 `call_next` 放行（material_digest D1, §7.5），属安全隐患；须改为**缺 token 即拒绝**（fail-closed），且明文配置缺失时启动告警，杜绝未授权访问。

#### 6.4.3 安全设计

- 提供认证授权功能：飞书验签 + Webhook Token + 可选 RBAC（守卫模块）；守卫含内网 IP 白名单、密钥策略、RBAC、红队对抗（高层 §6.2 守卫）。
- HITL 翻转、越权尝试均写入审计 WAL（N2）。

#### 6.4.4 安全开发

- 对函数入口参数（意图标签、agent_output、回调 payload）做合法性与格式校验；输入边界检查（长度 / 类型）。
- 飞书回调 payload 做适当过滤，防范恶意指令与内部信息泄露（如 agent_output 不外带敏感上下文）。
- 禁止引入未经授权和验证的代码；字符串改写源码脚本已停用（US-6），消除无类型检查文本 patch 引入的后门风险。

#### 6.4.5 安全测试和部署

- 上线前安全扫描（bandit / ruff 已配置）；安全配置基线检查（token 非空、fail-closed）。
- 红队测试接入评估门禁（Promptfoo，US-5）：提示注入 / 越权尝试被守卫拦截比例计入指标。
- 系统上线前不存在高危风险（fail-open 修复为 P1 安全项）。

#### 6.4.6 数据安全

- **存储与传输加密**：飞书 / Webhook 传输经 HTTPS；审计 WAL 含敏感操作但脱敏存储；PG 连接启用 TLS；密钥（WEBHOOK_AUTH_TOKEN / FEISHU_APP_SECRET / GITHUB_TOKEN）经环境变量 / 密钥管理注入，不落代码。
- **审计 100%**：所有关键操作（路由命中、守卫裁决、HITL 翻转、回投）写入审计 WAL，覆盖率 100%（N2），可供合规回放。

---

## 附录 C：中间确认协议自检（product-story-designer 适用）

> 依据中间确认协议 §2.1（方案分歧判定）+ §2.3（反向验证 3 问），在产出 §1~§6 关键章节后做自检，结果留存供主理人 G4 追溯。U-01/02/03 已在高层的 §4.3 冻结回退链，不重复发起；仅细化已冻结边界内的故事拆分不属未冻结分歧。

### C.1 §2.1 方案分歧判定（触发标准 #1）

判定标准：是否出现"≥2 方案均合理、影响下游、且用户/上游未冻结"的决策点？

- **候选分歧 1：意图路由用 semantic-router 库 vs 自研映射表**。高层 §4.3（D3）已冻结为"自研对齐映射表（不引库，先止血）"，semantic-router 仅作范式参考。已冻结，不发起。
- **候选分歧 2：向量库 pgvector vs Qdrant**。高层 §4.3（D3）已冻结为 pgvector（与 PG 同库），Qdrant 仅作完整版阈值触发项。已冻结，不发起。
- **候选分歧 3：飞书回调端点 /webhook/callback vs 仅回执**。高层 §5.2（关键约束）+ §4.3（U-03）已冻结为"必须指向 /webhook/callback，业务边界不变"。已冻结，不发起。
- **候选分歧 4：HITL 修复的超时阈值 / 升级分支细节**。属已冻结边界（F2 真闭环）内的故事细化，非未冻结平局；超时 24h 为借鉴 Dify/n8n 的默认取值，可在 G4 由 system-architect 微调，不影响本 US 拆分。

**结论：本阶段未命中 §2.1 触发标准**（所有取舍已在高层 G3 冻结，本 US 仅细化已冻结边界内的故事，无 ≥2 均合理且用户/上游未冻结的分歧）。

### C.2 §2.3 反向验证 3 问（强制）

| 问题 | 答案与证据 |
| --- | --- |
| Q1：这个决策若 3 个月后被推翻，工程返工成本可控吗？ | 本文档为 UserStory（行为描述），非代码改动；即便下游推翻某实现（如 pgvector→Qdrant），返工限于 system-architect 的存储适配，本阶段产物无需 30% 以上返工。 |
| Q2：这个决策的结果，用户 / 客户 / 监管能感知到吗？ | 能。US-1 使 Coder/Review 请求被正确分发（开发者可感知）；US-2 使审批结果真正回投（终端用户/合规可感知）；N2 审计 100% 留痕（监管可感知）。均为用户可见行为变化。 |
| Q3：这个决策与用户原始诉求中显式提及的能力是否一致？ | 一致。用户 D2 要求"复用现有代码、避免大规模重写"；本文档 US-6 停用字符串改写脚本、US-1~US-5 均为最小接线修正，直接落实该约束。 |

Q1、Q2、Q3 均未命中 §2.2 任一情形 → **无需发起 `[中间确认]`**。

### C.3 结论

本阶段（product-story-designer / Phase 4 UserStory）**全程未命中**中间确认协议的触发标准，故未向主理人发起 `[中间确认]` 阻塞消息。6 条 UserStory（US-1~US-6）均在高层的已冻结业务边界（D1~D5、In/Out-of-Scope、U-01/02/03）内细化，供 G4 审核与 system-architect 下游消费。
