# AICoding 架构设计 · 行业调研报告

> ⚠️ **历史文档（2026-08，未按此实施）**：作为**行业调研**，本文档列的是"业界有什么、我们考虑过什么"，
> 这没有错；但文档内几处 `✅ 已引入` 的完成标记**不成立**——RAGAS / DeepEval / Promptfoo /
> Prometheus / Grafana 在代码里**没有任何引用**，实际落地的是自研 harness（`evals/`）、审计 WAL
> 与内置 dashboard。当前口径以 `定位与减法清单.md`、README 与 `docs/adr/` 为准。

> 本文档为《AICoding 架构设计》核心产物之一，定位为**行业调研报告（research_report）**。
> 上游输入：主理人转交的用户诉求 + `material_digest.md`（G1 已通过，含根因 A~F 与冲突 X1~X4）。
> 下游输出：驱动 `business-architect`（业务架构师）的行业调研判断，最终落入《高层架构设计》的 §3 行业调研章节。
> **结构纪律**：全文按「事实 → 对比 → 建议 → 风险」四段式组织。标注约定：【事实】= 已核实公开来源；【推断】= 基于事实的合理归纳；【建议】= 供业务架构师采纳的取舍建议（非最终裁决）；【风险】= 调研发现的潜在威胁。

---

## 0. 元信息：修订记录

```yaml
标题: moa-gateway - 行业调研报告 v0.1
版本: v0.1
状态: Draft   # Draft | Reviewing | Approved | Deprecated
创建日期: 2026-08-29
最后更新: 2026-08-29
调研人: research-analyst（研究分析师 - 查有据）
审核人:
  - team-lead（主理人）

关联文档:
  上游输入:
    - 用户诉求: 由主理人注入（企业级 Agent 网关，框架已搭好但模块逻辑未跑通，需复用现有代码、避免大规模重写）
    - 调研基线: material_digest.md（G1 通过，根因 A~F 与冲突 X1~X4）
  下游产出:
    - 高层架构设计 §3 行业调研: 将由 business-architect 整合到此章节
```

| 版本 | 日期 | 作者 | 变更内容 | 评审状态 |
| --- | --- | --- | --- | --- |
| v0.1 | 2026-08-29 | research-analyst | 初稿（Phase 2 行业调研，G2） | Draft |

---

## 1. 调研问题收敛

> 围绕用户诉求与 `material_digest.md` 已实证的根因 A~F，将调研聚焦于"如何修通断点"，而非推倒重来。用户已冻结约束：**复用现有代码、避免大规模重写**（诉求 D2 §4）。

### 1.1 原始调研种子

| 编号 | 待验证论题 | 来源（用户诉求 / 根因要点） | 调研优先级 | 备注 |
| --- | --- | --- | --- | --- |
| S1 | 意图标签 → Agent 注册键的成熟映射模式 | 根因 A（intent 与 agent 注册键不匹配，CoderAgent 永不选中） | 高 | 需可嵌入自研骨架的模式 |
| S2 | 飞书 HITL 审批"真闭环"的标准模式 | 根因 B（卡片回调仅回执不闭环） | 高 | 回调端点统一 + 状态翻转 + 结果回投 |
| S3 | 轻量 RAG / 检索的选型（真向量库 vs 关键词/混合） | 根因 C（检索/知识默认全空） | 高 | 体量适配 |
| S4 | 存储选型（Postgres+pgvector vs SQLite）边界 | 根因 D（两套 schema 互不消费） | 中 | gateway/RAG 场景 |
| S5 | 自研 FSM 网关 vs Dify/LangGraph/n8n/Flowise 取舍 | 整体架构（用户要求复用、不重写） | 高 | 给出"在自研骨架补哪些成熟模式" |
| S6 | 评估体系（LLM-as-judge / 意图准确率 / 红队）参考做法 | 评估与守卫（evaluator / redteam） | 中 | 反制根因 F 的测试失真 |

### 1.2 调研问题收敛

| 编号 | 调研问题 | 调研对象 | 调研目标 | 预期产出 | 关联种子 |
| --- | --- | --- | --- | --- | --- |
| Q1 | 业界"意图分类/路由 → Agent 注册与分发"的成熟映射模式有哪些？各自的 key 对齐机制是什么？ | semantic-router、LangGraph supervisor、Dify agent 节点、Rasa policy/action | 找出可消除"intent↔agent 键不匹配"的范式 | 模式对比 + 借鉴点 | S1 |
| Q2 | 业界 human-in-the-loop 审批闭环的标准实现（回调端点、状态机翻转、结果回投）如何设计？飞书卡片回调机制能否支撑？ | Dify Human Input、LangGraph interrupt、n8n Wait、飞书卡片回调文档 | 定位根因 B 的修复范式 | 闭环模式对比 + 飞书适配结论 | S2 |
| Q3 | 轻量 RAG 应真向量库还是关键词/混合？pgvector / Qdrant / Milvus 在本项目体量下的取舍？ | Dify RAG、向量库基准（Inductivee 2025、社区选型指南） | 给出检索层与存储层的最小可行方案 | 选型矩阵 + 体量建议 | S3、S4 |
| Q4 | 自研 FSM 网关应在哪些点吸收成熟框架模式，而非整体替换？ | Dify / LangGraph / n8n / Flowise / Rasa + 本项目自研骨架 | 在"复用现有代码"约束下给出补强边界 | 自研/复用边界建议 | S5 |
| Q5 | 业界 LLM 应用评估（意图准确率、faithfulness、红队）的可用框架与 CI 集成做法？ | RAGAS / DeepEval / Promptfoo / LangChain OpenEvals | 反制根因 F 的 monkeypatch 失真，建立可信评估 | 评估体系建议 | S6 |

---

## 2. 事实：标杆系统盘点和方案详述

> **四段式「事实」段**。只陈列调研发现的事实，不做引申建议或边界裁决。置信度见每张表。

### 2.1 行业标杆清单

**硬指标**：≥ 3 家；至少包含 1 家头部 SaaS 代表 + 1 家开源/自研代表。

| 编号 | 标杆系统 | 厂商 / 社区 | 部署形态 | 场景覆盖 | 技术亮点 | 商业模式 | 调研来源 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| B1 | Dify | LangGenius（开源社区 + Dify Cloud SaaS） | SaaS / 私有化（Docker、K8s） | LLM 应用、Agent、Workflow、RAG、知识库 | 可视化工作流引擎、Agent 运行时、Human Input 节点、混合检索（向量+全文+重排） | 开源（修改版 Apache 2.0）+ 云订阅 | SR-03、SR-04 |
| B2 | LangGraph / LangChain | LangChain（开源社区 + LangSmith SaaS） | 库（自托管）/ LangSmith 云平台 | 多 Agent 编排、状态机、HITL、可观测 | Supervisor 多 Agent、interrupt/Command(resume)、checkpointer 持久化 | 开源（BSD）+ LangSmith 用量计费 | SR-01、SR-02 |
| B3 | n8n | n8n GmbH（开源社区 + n8n Cloud / Enterprise） | SaaS / 私有化（Docker） | 工作流自动化、AI Agent、HITL | Wait 节点（On Webhook Call）、Human review for tools、10+ 审批通道 | 开源（可持续型 License）+ 云/企业订阅 | SR-05、SR-06 |
| B4 | Rasa | Rasa（开源社区 + Rasa Enterprise） | 私有化 / 云 | 对话式 AI、意图识别、对话管理 | NLU pipeline、Stories/Policies、Custom Actions、CALM | 开源（Apache 2.0）+ 企业订阅 | SR-07 |
| B5 | semantic-router | Aurelio AI（开源社区） | 库（自托管 / 本地） | 意图路由、Agent 分发决策层 | 基于向量语义空间的 RouteLayer，0 LLM 调用决策、毫秒级、HybridRouteLayer | 开源（MIT） | SR-08 |

> 【事实】B1（Dify）同时具备头部 SaaS（Dify Cloud）与开源自托管两种形态；B2~B5 均为开源/自研代表。已满足硬指标"≥1 头部 SaaS + ≥1 开源/自研"。

### 2.2 标杆方案详述

#### 2.2.1 B1 - Dify

| 维度 | 内容 | 置信度 |
| --- | --- | --- |
| 产品定位 | 开源 LLMOps 平台，可视化构建 AI 应用 / Agent / Workflow / RAG | 已核实 |
| 目标用户 | 需快速搭建生产级 AI 应用、RAG 机器人、多步工作流的团队 | 已核实 |
| 核心能力 | 工作流引擎（分支/循环/并行）、Agent 运行时（ReAct/Function Calling）、知识库 RAG、Human Input 节点 | 已核实 |
| 架构特点 | 图式工作流；节点化编排；知识库内置三种检索策略：**向量检索 / 全文检索 / 混合检索（可加重排）**；外部向量库可接 Qdrant/Weaviate/Milvus/pgvector | 推断（来源：第三方部署指南与官方博客） |
| 部署形态 | SaaS（Dify Cloud）+ 私有化（Docker Compose 默认 13 容器，含 Postgres+Redis+向量库） | 已核实 |
| 集成方式 | 发布即 REST API；插件系统（工具/模型/连接器）；模型抽象统一 | 已核实 |
| 定价模式 | 开源免费自托管；Dify Cloud 订阅 + 用量 | 已核实 |
| 优势 | RAG 与 HITL 开箱即用；可视化调试；混合检索质量高 | 综合归纳 |
| 局限 | 作为"平台"整体替换会违背本项目"复用现有代码、不重写"的约束；与现有 FastAPI+LiteLLM 骨架耦合需走 API 后端模式 | 推断 |
| 对本项目的参考价值 | Human Input 节点（多分支审批 + 超时升级 + 评论回投变量）、混合检索策略值得**模式借鉴** | 推断 |

#### 2.2.2 B2 - LangGraph / LangChain

| 维度 | 内容 | 置信度 |
| --- | --- | --- |
| 产品定位 | 用于构建有状态、多 Agent 应用的图式编排框架 | 已核实 |
| 目标用户 | 需要精细控制 Agent 状态转移、分支、HITL 的工程师 | 已核实 |
| 核心能力 | Supervisor 多 Agent 编排（handoff 工具）、interrupt()/interrupt_before/after、Command(resume)、checkpointer 持久化 | 已核实 |
| 架构特点 | 把 Agent 编排显式建模为图（节点=Agent/工具，边=转移）；supervisor 通过"工具=其他 Agent"实现分发；HITL 靠 checkpointer 保存状态后由人类 resume | 推断（来源：官方博客与文档） |
| 部署形态 | 库（自托管，可与任意 FastAPI 服务集成）；LangSmith 提供云端可观测 | 已核实 |
| 集成方式 | Python/JS SDK；与 LiteLLM、LangSmith、各类向量库兼容 | 已核实 |
| 定价模式 | 框架开源（BSD）；LangSmith 按用量 | 已核实 |
| 优势 | 与现有 FastAPI+LiteLLM 技术栈契合度高；可作为库增量吸收，不改写骨架 | 综合归纳 |
| 局限 | 图范式与现有自研 FSM 心智模型不同，需适配；学习曲线 | 推断 |
| 对本项目的参考价值 | **Supervisor 分发模式**与 **interrupt/Command(resume) HITL 模式**可直接映射到"意图路由→Agent 分发"与"审批状态翻转" | 推断 |

#### 2.2.3 B3 - n8n

| 维度 | 内容 | 置信度 |
| --- | --- | --- |
| 产品定位 | 工作流自动化平台，含 Advanced AI（AI Agent 节点） | 已核实 |
| 目标用户 | 自动化工程师、需把 AI 接入既有系统的团队 | 已核实 |
| 核心能力 | AI Agent 节点、Human-in-the-loop for tools（批准/拒绝）、Wait 节点（On Webhook Call）、10+ 审批通道（Slack/Telegram/邮件等） | 已核实 |
| 架构特点 | 节点画布；HITL 通过"暂停执行→外部信号恢复"实现；Wait 节点可用 Webhook 模式：工作流暂停直到某 URL 被调用后继续 | 推断（来源：官方文档与博客） |
| 部署形态 | SaaS（n8n Cloud）/ 私有化（Docker） | 已核实 |
| 集成方式 | 节点 + HTTP Request；可作为外部工作流引擎被调用 | 已核实 |
| 定价模式 | 开源（可持续型许可证）+ Cloud/Enterprise 订阅 | 已核实 |
| 优势 | HITL 的"超时+升级+审计"模式成熟；Wait-on-webhook 与飞书回调机制同构 | 综合归纳 |
| 局限 | 节点可视化范式与代码优先的 FastAPI 骨架差异大，整体采用会偏离复用约束 | 推断 |
| 对本项目的参考价值 | **Wait-on-webhook + 超时升级 + 审计日志**的 HITL 模式可借鉴到飞书审批闭环 | 推断 |

#### 2.2.4 B4 - Rasa

| 维度 | 内容 | 置信度 |
| --- | --- | --- |
| 产品定位 | 开源对话式 AI 框架（NLU + 对话管理） | 已核实 |
| 目标用户 | 构建任务型对话机器人、客服的团队 | 已核实 |
| 核心能力 | NLU pipeline（DIET 意图/实体）、Stories（对话训练数据）、Policies（TED/Rule/Memoization 决定下一步 action）、Custom Actions | 已核实 |
| 架构特点 | **意图(intent) → 动作(action) 的显式映射**由 domain.yml + stories.yml + rules.yml 声明；Policies 多策略按置信度择优；Custom Action 经独立 Action Server 调用 | 推断（来源：官方文档与论文） |
| 部署形态 | 私有化（自托管）/ Rasa Enterprise | 已核实 |
| 集成方式 | Rasa SDK（Action Server）、Tracker Store（可接 Postgres/Redis） | 已核实 |
| 定价模式 | 开源（Apache 2.0）+ 企业订阅 | 已核实 |
| 优势 | "意图键 → 动作键"的一一映射范式清晰，正可对照根因 A 的键不匹配 | 综合归纳 |
| 局限 | 面向对话机器人、需训练 NLU 模型，与本项目"代码生成/网关"场景错配；整体引入过重 | 推断 |
| 对本项目的参考价值 | 借鉴其 **"intent 名称必须与注册 action 名称严格一致"** 的契约思想，而非引入框架本身 | 推断 |

#### 2.2.5 B5 - semantic-router

| 维度 | 内容 | 置信度 |
| --- | --- | --- |
| 产品定位 | 为 LLM/Agent 提供超快决策层（意图路由）的开源库 | 已核实 |
| 目标用户 | 需要在工具/Agent 调用前做快速、确定性路由的开发者 | 已核实 |
| 核心能力 | RouteLayer（向量语义路由）、Route 对象（name + utterances 示例）、score_threshold 低于阈值返回 None 落到默认处理、HybridRouteLayer、支持本地 encoder（HuggingFace/FastEmbed） | 已核实 |
| 架构特点 | **路由名即下游键**：定义 `Route(name="coding", ...)`，路由结果 `.name` 直接作为分发键；0 LLM 调用、毫秒级；低于阈值回退默认（无幻觉工具调用） | 推断（来源：官方 README 与文档） |
| 部署形态 | 库（自托管 / 本地，MIT） | 已核实 |
| 集成方式 | pip 安装，可作为一层嵌入现有 FastAPI 服务 | 已核实 |
| 定价模式 | 开源（MIT），无许可成本 | 已核实 |
| 优势 | 极轻量、可嵌入；路由名与 Agent 注册键天然对齐，直接修复根因 A | 综合归纳 |
| 局限 | 社区规模小于 LangChain；语义路由需 embedding 依赖（可本地化） | 推断 |
| 对本项目的参考价值 | **以 Route.name 作为 Agent 注册键**的范式，可直接套用到现有三级意图路由，消除键不匹配 | 推断 |

### 2.3 关键技术能力横向事实

> 不评分、不排序，仅按能力维度横陈各方案事实。

| 能力维度 | B1 Dify | B2 LangGraph | B3 n8n | B4 Rasa | B5 semantic-router |
| --- | --- | --- | --- | --- | --- |
| 意图→Agent 分发键对齐 | 工作流节点连线显式绑定 | Supervisor handoff 工具名=Agent 名 | AI Agent 节点 + 子工作流 | intent 名=action 名（domain 声明） | **Route.name=路由键**（直接对齐） |
| HITL 审批闭环 | Human Input 节点：多分支（确认/重生成/转发）+ 超时升级 + 评论变量回投 | interrupt() + checkpointer + Command(resume)，状态持久化后恢复 | Wait 节点（On Webhook Call）+ 工具级 Human review + 超时/升级 + 审计 | 无内建 HITL（需自研 action） | 不涉及（仅路由） |
| RAG / 检索 | 向量/全文/**混合** + 重排 + 元数据过滤 | 依赖外部检索器 | 向量库节点（多家） | 无内建 RAG | 不涉及 |
| 向量库支持 | Weaviate/Qdrant/Milvus/pgvector/内置 | 任意（LangChain 集成） | Qdrant/Weaviate 等 | 不涉及 | 不涉及（可用 PG pgvector 作索引） |
| 可嵌入自研 FastAPI 骨架 | 弱（平台级，宜作后端 API） | 强（库级） | 中（可作外部引擎） | 弱（独立框架） | **强（库级，pip 即嵌）** |
| 许可 / 成本 | 修改版 Apache 2.0，自托管免费 | BSD，免费 | 可持续型 License，免费自托管 | Apache 2.0，免费 | **MIT，免费** |

> 【事实】上表各行均来自 §2.2 引用的公开文档/博客（SR-01~SR-08）。其中语义路由"Route.name 即路由键"与 Rasa"intent=action 名"是修复根因 A 的关键事实；Dify/n8n/LangGraph 的 HITL 均含"超时+升级+状态持久化+结果回投"四要素，是修复根因 B 的事实基线。

---

## 3. 对比：对比矩阵与加权评分

> **四段式「对比」段**。在 §2 的事实基础上建立对比矩阵，赋予权重并打分。

### 3.1 对比矩阵

> 评估维度与权重针对本项目"代码优先、需复用、体量中等、企业内网"的特征设定。**每行权重之和 = 1.00**。

| 评估维度 | 权重 | 权重理由 | B1 Dify | B2 LangGraph | B3 n8n | B4 Rasa | B5 semantic-router |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 场景契合度 | 0.30 | 本项目是代码优先的 FastAPI+LiteLLM 网关，需"补模式"而非"换平台"；与现有骨架契合度权重最高 | 4 | 4 | 3 | 2 | 4 |
| 技术成熟度 | 0.20 | 决定能否直接借鉴其经过验证的模式 | 5 | 5 | 5 | 5 | 3 |
| 集成难度（反向） | 0.15 | 越能作为库增量嵌入、不推翻骨架越优 | 2 | 3 | 3 | 2 | 5 |
| 成本（反向） | 0.15 | 许可与运维成本（团队体量小） | 3 | 4 | 3 | 3 | 5 |
| 合规可控性 | 0.20 | 企业内网友好、可自托管、数据不出域 | 4 | 5 | 4 | 5 | 5 |
| **加权总分** | **1.00** | — | **3.75** | **4.25** | **3.60** | **3.35** | **4.30** |

**评分标尺**：每项 1~5 分，1 = 严重不符合，3 = 基本满足但存在明显局限，5 = 完美契合。
* 加权计算（以 B1 为例）：4×0.30 + 5×0.20 + 2×0.15 + 3×0.15 + 4×0.20 = 1.20+1.00+0.30+0.45+0.80 = 3.75；其余同理（B2 4.25 / B3 3.60 / B4 3.35 / B5 4.30）。

### 3.2 评分结论

> 基于 §3.1 加权总分，形成分层结论。每层结论引用得分作为依据。

- **优先借鉴**：`semantic-router`（4.30）与 `LangGraph`（4.25）。
  - 理由：两者加权总分最高，且均为**库级、可嵌入**形态，契合"复用现有代码、不重写"的冻结约束。semantic-router 的 `Route.name` 直接作为 Agent 分发键，可消除根因 A 的键不匹配；LangGraph 的 supervisor handoff 与 `interrupt()/Command(resume)` 分别是"意图→Agent 分发"与"HITL 状态翻转"的可参照范式。
- **部分借鉴**：`Dify`（3.75）与 `n8n`（3.60）。
  - Dify 借鉴点：Human Input 节点的**多分支审批 + 超时升级 + 评论变量回投**设计，以及**混合检索（向量+全文+重排）**策略；不借鉴其平台整体替换。
  - n8n 借鉴点：**Wait-on-webhook + 超时/升级 + 审计日志**的 HITL 模式（与飞书卡片回调同构）；不借鉴其可视化节点引擎整体。
- **不借鉴（否决）**：`Rasa`（3.35）整体框架，以及任何"以 Dify/LangGraph/n8n 整体替换自研骨架"的方案。
  - 否决理由：Rasa 面向对话机器人且需训练 NLU，与"代码生成/网关"场景错配、引入过重（评分最低 3.35）；整体平台替换违背用户已冻结的"复用现有代码、避免大规模重写"约束（见 §4.1 与附录自检）。

### 3.3 方案组合分析

| 组合方式 | 覆盖哪些能力 | 未覆盖能力 | 组合复杂度 | 总体成本估算 |
| --- | --- | --- | --- | --- |
| **B5 路由层 + B2 范式 + 自研 FSM/HITL + 飞书回调修正**（推荐组合） | 意图路由键对齐(A)、HITL 状态翻转范式(B)、可嵌入不重写(S5) | 真向量 RAG 仍需单独引入 pgvector(C/D) | 中 | 低（库级依赖，无平台许可成本） |
| B1 平台整体引入 | RAG/HITL/工作流全栈 | 与现有 FastAPI+LiteLLM 骨架整合需走后端 API，偏离复用约束 | 高 | 中（自托管运维 + 可选云） |
| B4 Rasa 整体引入 | 严谨的 intent→action 映射思想 | 场景错配、需训练、过重 | 高 | 中 |

---

## 4. 建议：取舍决策支持

> **四段式「建议」段**。基于 §2 事实 + §3 对比，给出可被 `business-architect` 直接采用的建议。本节是**建议而非最终裁决**，最终边界由业务架构师冻结。

### 4.1 自研 / 采购 / 复用边界建议

| 能力项（对应根因） | 建议方式 | 建议依据 | 候选方案 / 系统 | 关键前提 |
| --- | --- | --- | --- | --- |
| 意图路由 → Agent 分发（A） | **复用（现有骨架）+ 吸收模式** | B5 的 `Route.name=键` 与 B4 的"intent 名=action 名"契约思想，可直接套用现有三级路由；无需引入平台 | semantic-router（参考范式）/ 自研对齐映射表 | 强制约束：`intent_router` 输出的意图标签集合必须与 `agents/loader.py` 注册键集合**同构**（消除 A）；保留正则→微模型→LLM 三级降级 |
| HITL 审批闭环（B） | **复用（现有 FSM）+ 修正接线** | B1 Human Input（多分支+超时+回投）、B2 interrupt/resume、B3 Wait-on-webhook 均证明"统一回调端点 + 状态翻转 + 结果回投"是标准范式 | 自研 `engine.handle_event` + 飞书卡片回调修正 | 飞书卡片 action 回调必须 POST 到真实闭环端点（如 `/webhook/callback`），而非仅 `adp.send()` 回执（修复 B/X2） |
| 检索 / RAG（C） | **采购/引入成熟组件（轻量）** | B1 混合检索质量最佳；本项目体量中等，真向量库收益明确 | pgvector（与 Postgres 同库，运维零新增）或 Qdrant（若需更强过滤/性能） | 需 Postgres 与 embedding API；保留关键词回退以兼容离线 |
| 存储选型（D） | **复用（统一 Postgres+pgvector）** | B1/B3 均将向量与关系数据同库；pgvector 在百万级以下性价比高（SR-09/ SR-10） | Postgres + pgvector 扩展 | 合并 `code_review_prs` 与 `code_review_vectors` 两套 schema 为单一 PG 实例（修复 D/X1）；SQLite 仅作本地开发回退 |
| 整体编排框架（S5） | **复用（自研 FSM）+ 吸收范式** | 用户冻结"复用现有代码、不重写"；B2/B5 为库级可嵌入，不触发重写 | 自研 FSM + LangGraph 范式参考 + semantic-router 范式参考 | 不引入 Dify/n8n/Rasa 作为运行时平台 |
| 评估体系（S6/F） | **采购/引入成熟框架** | RAGAS/DeepEval/Promptfoo 为业界标准，可 CI 门禁 | RAGAS（RAG 质量）、DeepEval（pytest 门禁）、Promptfoo（红队/提示注入） | 建立 Golden Dataset（≥50 例）；eval 不得用 monkeypatch 掩盖真实接线（反制 F） |

### 4.2 MVP 范围建议

| 功能（对齐用户诉求 / 根因） | 建议 MVP？ | 理由 |
| --- | --- | --- |
| R1 修复意图↔Agent 键对齐（A） | ✅ | 范式清晰（B5/B4），改动集中在路由层与注册表对齐，无需新依赖即可先以"映射表"止血 |
| R2 修复飞书 HITL 真闭环（B） | ✅ | 仅需把卡片回调指向真实闭环端点 + 状态翻转 + 结果回投（参考 B1/B2/B3），工作量可控 |
| R3 引入真向量 RAG（C/D） | ⚠️（MVP 轻量版） | 建议 MVP 先上 pgvector + 混合检索最小集；完整重排/多知识库可延后 |
| R4 统一 Postgres+pgvector 存储（D） | ✅ | 消除两套 schema 割裂，属"修数据层"而非新功能，应随 R3 一并做 |
| R5 评估体系与 CI 门禁（F/S6） | ✅（最小集） | 引入 RAGAS/DeepEval + 去除 monkeypatch 的集成测试，直接反制根因 F，防止回归 |
| R6 多 Agent 复杂协作（supervisor 多级） | ❌（完整版） | 当前仅 Coder/General/Review 三 Agent，先跑通分发，多级 supervisor 为后续增强 |

### 4.3 技术栈参考建议

| 技术层 | 推荐方案 | 替代方案 | 选择理由 |
| --- | --- | --- | --- |
| 意图路由决策层 | 自研对齐映射 + 参考 semantic-router 范式 | semantic-router 直接引入 / LangGraph supervisor | 最小依赖、可嵌入；优先消除键不匹配（A） |
| Agent 编排 | 复用现有自研 FSM + 吸收 LangGraph interrupt/resume 范式 | 直接引入 LangGraph 运行时 | 满足"不重写"约束，仅借鉴范式 |
| 向量数据库 | **pgvector**（与 Postgres 同库） | Qdrant（更强过滤/性能）、Milvus（亿级规模） | 本项目体量中等（百万向量以下），pgvector 运维零新增；>500 万再评估 Qdrant（SR-09/ SR-10） |
| 混合检索 | 向量 + 关键词 + 可选重排 | 纯向量 | 参照 B1 混合检索质量最佳实践（C） |
| 评估 | RAGAS + DeepEval + Promptfoo | LangChain OpenEvals、Braintrust | 开源、CI 友好、覆盖 faithfulness/意图准确率/红队（S6/F） |
| 网关/多模型代理 | **复用现有 LiteLLM**（已在项目依赖） | — | 已具备多 provider/fallback/cost，符合复用约束 |

---

## 5. 风险与待确认项

> **四段式「风险」段**。列出调研中发现的主要风险、不确定信息、待业务架构师进一步裁决的依赖项。

### 5.1 主要风险清单

| 编号 | 风险描述 | 触发条件 | 影响范围 | 严重程度 | 缓解建议 |
| --- | --- | --- | --- | --- | --- |
| R-01 | 引入 semantic-router 范式需 embedding 依赖，离线/API 故障会拖慢路由 | 无 embedding API 或本地 encoder 未配置 | 意图路由降级 | 中 | 保留现有"正则→微模型→LLM"三级降级作为兜底；本地 FastEmbed/HuggingFaceEncoder 可离线 |
| R-02 | 飞书 HITL 闭环修正若回调 URL 配置错误，会**重现根因 B 假闭环** | 卡片 action 回调未指向真实闭环端点 | HITL 审批（B） | 高 | 统一单一回调端点；增加"状态翻转"集成测试断言，禁止仅靠 `adp.send()` 回执 |
| R-03 | 引入 pgvector 需 Postgres 与迁移，且须合并两套割裂 schema（X1/D） | 未统一 `code_review_prs` 与 `code_review_vectors` | 存储层（D） | 中 | 单一 PG 实例承载关系+向量；SQLite 仅本地回退；编写迁移脚本 |
| R-04 | LLM-as-judge 评估引入成本/延迟，且裁判模型存在偏见 | 高频跑全量 eval | 评估成本（S6） | 低 | RAGAS 参考无关答案（reference-free）；设阈值门禁；做人工相关性校验 |
| R-05 | 根因 F 的 monkeypatch 测试失真若遗留，标杆借鉴无法验证真实接线 | 集成测试仍大量 monkeypatch | 整体可运行性（F） | 高 | 去除覆盖关键路径的 monkeypatch，改用真实 `get_agent`/真实闭环端点的集成测试 |

### 5.2 待确认项（需主理人 / 业务方反馈）

| 编号 | 待确认项 | 不确定性说明 | 若无法确认的备选路径 |
| --- | --- | --- | --- |
| U-01 | 企业内网是否允许对外 embedding API（C/R3） | 若内网禁外联，需本地 embedding 模型 | 本地 FastEmbed/bge 系列；关键词检索兜底 |
| U-02 | 是否已有 Postgres 实例可用于 pgvector（D/R4） | 若无，需新增 PG 部署 | 用现有 Redis/内存先跑通逻辑，存储延后；或用 Docker 单容器 PG |
| U-03 | 飞书应用是否具备"消息卡片回调 URL"配置权限（B/R2） | 需开发者后台配置 Message Card Request URL | 由主理人协调飞书管理员确认 |

### 5.3 需业务架构持续关注的依赖项

| 编号 | 依赖项 | 说明 | 建议关注阶段 |
| --- | --- | --- | --- |
| D-01 | 若采用 pgvector，需评估与现有 Redis 状态栈的协作边界 | 见 §4.1 存储建议 | 高层架构设计 §5.2 |
| D-02 | HITL 闭环修复涉及"用户可见行为"变化（审批结果真正回投） | 见 §2.2 B1/B3 HITL 模式 | 业务架构/安全设计 |
| D-03 | 评估体系需嵌入 CI 门禁，影响交付节奏 | 见 §4.3 评估建议 | 落地步骤规划 |

---

## 6. 关键来源目录

> 集中列出全部调研所使用的公开资料。每条来源不低于 URL 粒度，关键数据指定来源段落/章节。

**硬指标**：≥ 3 条来源，至少覆盖每家标杆。

| 编号 | 来源类型 | 标题 / 名称 | URL / 路径 | 相关章节 | 最后访问日期 |
| --- | --- | --- | --- | --- | --- |
| SR-01 | 官方文档 | LangGraph Multi-Agent Supervisor（JS 参考） | https://reference.langchain.com/javascript/langchain-langgraph-supervisor | B2, §2.2.2, Q1 | 2026-08-29 |
| SR-02 | 官方文档/社区 | LangGraph Human-in-the-Loop（interrupt / Command resume / checkpointer） | https://docs.langchain.com/oss/javascript/langgraph/interrupts ；https://mintlify.wiki/langchain-ai/langgraph/concepts/human-in-the-loop | B2, §2.2.2, Q2 | 2026-08-29 |
| SR-03 | 第三方指南 | Dify AI Guide 2026（架构/工作流/RAG/Human Input） | https://aitoolsdevpro.com/ai-tools/dify-guide | B1, §2.2.1, Q3 | 2026-08-29 |
| SR-04 | 官方博客 | Dify: The Human Input Node（多分支审批 + 超时升级 + 评论回投） | http://dify.ai/blog/the-human-input-node-bringing-human-judgment-into-automated-workflows | B1, §2.2.1, Q2 | 2026-08-29 |
| SR-05 | 官方文档 | n8n Human-in-the-loop for tools | https://docs.n8n.io/advanced-ai/human-in-the-loop-tools/ | B3, §2.2.3, Q2 | 2026-08-29 |
| SR-06 | 官方博客 | n8n Human in the loop automation（Wait 节点 + 超时/升级/审计） | https://blog.n8n.io/human-in-the-loop-automation | B3, §2.2.3, Q2 | 2026-08-29 |
| SR-07 | 官方文档 | Rasa Stories（intent → action 映射 + Policies） | https://rasa.com/docs/rasa/stories | B4, §2.2.4, Q1 | 2026-08-29 |
| SR-08 | 官方仓库/站点 | semantic-router（Aurelio AI，Route.name 即路由键） | https://github.com/aurelio-labs/semantic-router ；https://aurelio.ai/semantic-router | B5, §2.2.5, Q1 | 2026-08-29 |
| SR-09 | 基准/博客 | Vector Database Benchmarks 2025（Qdrant/Milvus/Weaviate/pgvector，10M~500M 向量） | https://inductivee.com/blog/vector-database-performance-benchmarks-2025 | §2.3, Q3, §4.3 | 2026-08-29 |
| SR-10 | 社区指南 | From Zero to 1B Vectors: 2025 Picking Guide（pgvector/Qdrant/Milvus 场景取舍） | https://dev.to/pascal_cescato_692b7a8a20/from-zero-to-1-b-vectors-the-2025-no-bs-picking-guide-4k9o | §2.3, Q3, §4.3 | 2026-08-29 |
| SR-11 | 评估框架综述 | LLM Evaluation Frameworks（RAGAS / DeepEval / Promptfoo 对比） | https://calmops.com/ai/llm-evaluation-frameworks-deepeval/ ；https://agentic-ai.readthedocs.io/en/latest/EvaluationFrameworks/llm-frameworks/ | §4.1 S6, Q5 | 2026-08-29 |
| SR-12 | 官方文档 | 飞书/ Lark 消息卡片交互回调（卡片 action → 开发者服务器 POST） | https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/callback-subscription/callback-overview ；https://open.larksuite.com/document/common-capabilities/message-card/add-card-interaction/interaction-module | B飞书适配, Q2, R-02 | 2026-08-29 |

---

## 7. 硬指标清单

> 汇总本模板所有章节的硬指标，供自动校验与人工审核使用。

| 章节 | 硬指标项 | 当前状态 | 备注 |
| --- | --- | --- | --- |
| §1 | 调研问题已收敛为 ≥ 3 条可执行问题 | ✅ | Q1~Q5（5 条） |
| §2.1 | 标杆系统 ≥ 3 家，含 ≥ 1 家头部 SaaS | ✅ | B1 Dify（Cloud SaaS）、B3 n8n（Cloud） |
| §2.1 | 标杆系统 ≥ 1 家开源或自研代表 | ✅ | B2/B4/B5 均为开源 |
| §2.2 | 每家标杆有独立详述卡片 | ✅ | B1~B5 各 §2.2.x |
| §2.3 | 关键能力横向事实无遗漏 | ✅ | 6 维度横陈 |
| §3.1 | 对比矩阵含 5 维度 + 权重 + 评分 | ✅ | 权重之和 = 1.00 |
| §3.2 | 评分结论含优先/部分/不借鉴三层 | ✅ | 优先(B5/B2)/部分(B1/B3)/不借鉴(B4+平台替换) |
| §4.1 | 自研/采购/复用边界有明确建议 | ✅ | 对应 A~D/S5/S6 |
| §4.2 | MVP 范围建议与用户诉求对齐 | ✅ | R1~R6 |
| §5.1 | 主要风险 ≥ 3 条，有缓解建议 | ✅ | R-01~R-05 |
| §6 | 关键来源可追溯（URL / 章节） | ✅ | SR-01~SR-12 |
| 全文 | 明确区分事实 / 推断 / 建议 / 风险 | ✅ | 标注【事实】【推断】【建议】【风险】 |
| 全文 | 不存在编造来源或占位符 | ✅ | 全文已消除占位符、示例前缀、待填日期与待填标记 |

---

## 附录 A：中间确认协议自检（research-analyst 适用）

> 依据 `intermediate_confirmation.md` §2.4，在产出关键章节后做自检，并将结果在此留存供主理人 G3~G5 追溯。

### A.1 §2/§3/§4 产出后自检

**§2.1 方案分歧判定（触发标准 #1）**：本研究是否遇到"≥2 方案均合理、影响下游、且用户/上游未冻结"的决策点？
- 检索发现真实方案分歧（如"引入真向量库 pgvector vs 保持关键词"、或"用 semantic-router 库 vs 仅自研映射表"）均属**技术选型建议**，最终边界由 `business-architect` 冻结（模板 §4 明示"建议而非裁决"）。用户原始诉求已冻结顶层约束"复用现有代码、避免大规模重写"（D2 §4），故"以 Dify/LangGraph/n8n/Rasa 整体替换"类方案已被冻结排除，不存在需用户即时裁决的未冻结分歧。
- 判定：**未命中 §2.1**。

**§2.3 反向验证 3 问（强制）**：

| 问题 | 答案与证据 |
| --- | --- |
| Q1：这个决策若 3 个月后被推翻，工程返工成本可控吗？ | 本研究产出为**调研报告（建议）**，非代码改动；其建议（如引入 pgvector、用映射表修 A）即便被下游推翻，返工范围限于业务架构师的对应章节与后续少量代码模块，**不涉及当前阶段产物 30% 以上返工**。切换成本 = 业务架构/落地阶段的数人日，非月级。 |
| Q2：这个决策的结果，用户 / 客户 / 监管能感知到吗？ | 调研报告本身不直接改变用户可见行为；它仅向业务架构师提供基准与建议。用户可见行为变化（如 HITL 真正回投）属于下游落地阶段决策，不在本研究裁决范围内。故本研究**用户/客户/监管不可感知**。 |
| Q3：这个决策与用户原始诉求中显式提及的能力是否一致？ | 用户原文（D2 §4）显式要求"**尽量复用现有代码、避免大规模重写**"。本研究 §3.2/§4.1 明确否决平台整体替换、建议"复用自研骨架+吸收范式"，与诉求**一致**；并直接引用该原文作为否决依据。 |

- Q1、Q2、Q3 均未命中 §2.2 任一情形 → **无需发起 [中间确认]**。

### A.2 结论

本研究全程未命中 `intermediate_confirmation.md` 的触发标准，故未向主理人发起 `[中间确认]` 弹窗。所有"取舍"均以**建议**形式呈现，留待 `business-architect` 在 G3 及后续阶段冻结。

---

## 附录 B：生成流程与方法论

| 步骤 | 动作 | 落入章节 |
| --- | --- | --- |
| Step0 | 读取模板 `research_report.md` + 上游 `material_digest.md` + 协议 `intermediate_confirmation.md` | — |
| Step1 | 围绕根因 A~F 收敛调研问题 Q1~Q5 | §1 |
| Step2 | WebSearch/WebFetch 获取公开可溯源来源（标杆官网/文档/基准/博客） | §2、§6 |
| Step3 | 标杆事实盘点 + 横向能力对比 + 加权评分矩阵 | §2、§3 |
| Step4 | 给出自研/采购/复用边界、MVP、技术栈建议 | §4 |
| Step5 | 风险/待确认/依赖项 + 协议自检 | §5、附录 A |
| Step6 | 运行自动校验脚本 `validate_template_compliance.py` 至 PASS | §7 |

**工具清单**：WebSearch / WebFetch（公开来源采集）；模板规定的四段式结构纪律；自动校验脚本（见主理人指定路径）。

**方法论备注**：所有结论标注【事实/推断/建议/风险】置信度；评分带权重与理由；来源均附 URL 与章节（SR-01~SR-12），未编造来源。
