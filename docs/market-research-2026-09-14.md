# 方向市场调研（2026-09-14）

> 目的：为 moa-gateway 重构选择垂直落地方向，遵循"有成熟产品就不重复造轮子"原则。
> 来源：所有 star 数据均为 2026-09-14 通过 GitHub Search API / Repo API 实查；商业产品仅标注存在，不列内部指标。

## 结论摘要

1. **AI Code Review、LLM Gateway/Guard、通用 Agent 工作流、个人 AI 助手均为红海，直接排除**。其中 LiteLLM（58,706★）已经覆盖 moa-gateway 的 Provider fallback + 成本核算 + Guardrails，是最典型的"重复造轮子"证据。
2. **AI 学习助手、内容创作/审批/发布在开源侧很弱，但商业侧都由成熟产品或大厂占据**，个人开发者进入没有差异化，排除。
3. **相对空白的方向只有一个：中文合同审查（本地部署 / 个人与小微 / 风险提示定位）**。英文侧已有企业级商业产品（Harvey、Spellbook）和 Claude legal skill（1,733★），但中文开源侧专项项目全部低于 150★，没有形成可用产品。

---

## 1. AI Code Review / PR 审查 —— 红海

| 项目 | Stars | 说明 |
|---|---|---|
| tirth8205/code-review-graph | 31,403 | AI code review |
| anthropics/claude-code-security-review | 6,218 | Anthropic 官方 AI 安全审查 Action |
| builderz-labs/mission-control | 6,215 | AI code review 平台 |
| darrenhinde/OpenAgentsControl | 4,843 | 含 automatic code review |

来源：
- https://github.com/tirth8205/code-review-graph
- https://github.com/anthropics/claude-code-security-review
- https://github.com/builderz-labs/mission-control
- https://github.com/darrenhinde/OpenAgentsControl

**判定**：大厂官方已入场，开源霸主已存在；本仓库的 `apps/code_review_pipeline/` 只能作为组件，不能作为产品方向。

---

## 2. LLM Gateway / Guardrails —— 红海，且与本项目核心功能高度重合

| 项目 | Stars | 说明 |
|---|---|---|
| BerriAI/litellm | 58,706 | Provider 统一接入 + fallback + 成本核算 + Guardrails |
| Portkey-AI/gateway | 12,987 | 50+ AI Guardrails |
| guardrails-ai/guardrails | 7,409 | Guard 框架 |
| NVIDIA-NeMo/Guardrails | 7,120 | 企业 Guard 框架 |
| katanemo/plano | 7,048 | 策略/权限 |
| superagent-ai/superagent | 6,747 | prompt injection / data leak 防护 |

来源：
- https://github.com/BerriAI/litellm
- https://github.com/Portkey-AI/gateway
- https://github.com/guardrails-ai/guardrails
- https://github.com/NVIDIA-NeMo/Guardrails
- https://github.com/katanemo/plano
- https://github.com/superagent-ai/superagent

**判定**：LiteLLM 一个项目就覆盖了 moa-gateway 现在最引以为傲的多 Provider fallback、成本核算、Guard 策略。继续做通用网关 = 重复造轮子。

---

## 3. 通用 Agent 工作流 / HITL 平台 —— 红海（n8n / Dify / Coze）

| 项目 | Stars | 说明 |
|---|---|---|
| n8n-io/n8n | 204,261 | 可视化工作流 + 400+ 集成 + 自托管 |
| langgenius/dify | 155,685 | Agentic workflows + RAG pipelines + 自托管 |
| coze-dev/coze-studio | 21,585 | Coze 开源版，全平台 Agent 创作 |
| manor-os/manor-ai | 172 | 自托管 AI Agent 工作区 + 人工审批（起步阶段） |

来源：
- https://github.com/n8n-io/n8n
- https://github.com/langgenius/dify
- https://github.com/coze-dev/coze-studio
- https://github.com/manor-os/manor-ai

**判定**："多渠道接入 + 状态机 + 人工审批"是 n8n/Dify/Coze 的通用能力。自托管 HITL 方向（manor-ai 等）仍在起步，但随时会被平台型产品覆盖，个人开发者没有生态优势。

---

## 4. 个人 AI 助手 / 事务管家 —— 红海

| 项目 | Stars | 说明 |
|---|---|---|
| khoj-ai/khoj | 37,328 | 自托管个人 AI 第二大脑 |
| siddsachar/row-bot | 1,491 | 本地优先个人 AI |
| matthiasn/lotti | 1,173 | 个人日志 + AI 建议 + 人工审批（与 HITL 模式重合） |

来源：
- https://github.com/khoj-ai/khoj
- https://github.com/siddsachar/row-bot
- https://github.com/matthiasn/lotti

**判定**：个人助手是自托管领域最拥挤赛道，且有成熟 HITL 产品（lotti）。没有切入点。

---

## 5. AI 学习助手 —— 开源弱，商业强，排除

开源侧最高星：
- KartikLabhshetwar/mind-mentor：146★
- mhss1/AIStudyAssistant：113★
- A-R007/Multi-Agent-Study-Assistant：57★

来源：
- https://github.com/KartikLabhshetwar/mind-mentor
- https://github.com/mhss1/AIStudyAssistant
- https://github.com/A-R007/Multi-Agent-Study-Assistant

**判定**：开源生态不成熟，但商业侧被 Khanmigo、Quizlet、Notion AI 等大厂/成熟产品占据；且学习助手是 to-C 产品，与开发者资产不匹配。

---

## 6. 内容创作 / 审批 / 发布 —— 商业成熟，开源全是 n8n 小工作流，排除

开源侧最高星仅为 Freespirits/social-auto-engine（25★），其余均为 n8n 模板级项目（LinkedIn/Facebook/SEO 等 1-8★）。

商业侧：Buffer、Hootsuite、Jasper、Copy.ai 等已覆盖"AI 生成 + 审批 + 多平台发布"。

来源（GitHub 搜索 `content approval workflow ai`）：
- https://github.com/Freespirits/social-auto-engine
- https://github.com/florinel-chis/mage-seo

**判定**：商业模式是 SaaS 红海；中文平台（小红书/抖音）缺乏开放发布 API，个人开发者做不成完整闭环。

---

## 7. 中文 IM 渠道（飞书/钉钉）—— 有玩家，且被平台型产品覆盖

| 项目 | Stars | 说明 |
|---|---|---|
| deepcoldy/botmux | 1,431 | 飞书/Lark 桥接 AI coding CLI |
| kai648846760/iflow-bot | 219 | 全平台 AI bot（飞书/钉钉/QQ/TG） |

来源：
- https://github.com/deepcoldy/botmux
- https://github.com/kai648846760/iflow-bot

**判定**：渠道接入本身不是差异化；Coze/Dify 已原生支持飞书/钉钉/微信，本仓库的飞书通道只能作为组件复用。

---

## 8. 合同审查 —— 唯一相对空白方向

英文/通用侧：

| 项目 | Stars | 说明 |
|---|---|---|
| zubair-trabzada/ai-legal-claude | 1,733 | 法律审查 Claude skill |
| evolsb/claude-legal-skill | 441 | 法律 skill |
| xiaodingfang/contract-review | 149 | 中文合同审查 Web 应用 |
| vikashjeyaraman/opencouncil-contract-inspector | 122 | Multi-Agent 合同审查 |

中文专项侧（GitHub 搜索 `contract review chinese`）全部低于 10★，均为 Claude Code skill 或单文件工具，没有产品化项目：
- XimilalaXiang/Law：6★
- lotushj1/claude-skill-contract-reviewer：4★
- woaiwangcai/contract-reviewer：3★

来源：
- https://github.com/zubair-trabzada/ai-legal-claude
- https://github.com/evolsb/claude-legal-skill
- https://github.com/xiaodingfang/contract-review
- https://github.com/vikashjeyaraman/opencouncil-contract-inspector
- https://github.com/XimilalaXiang/Law
- https://github.com/lotushj1/claude-skill-contract-reviewer
- https://github.com/woaiwangcai/contract-reviewer

**判定**：
- 企业级商业产品（Harvey、Spellbook）存在，说明付费意愿真实，但价格高、面向法务团队。
- 中文开源侧没有"本地部署 + 个人/小微可用 + 风险提示定位"的成熟产品，存在真实空白。
- 风险必须正视：不能输出"法律意见"（执业风险），产品必须定位为"摘要 + 风险点提示 + 与自有模板比对 + 人工复核"。

---

## 与 moa-gateway 现有资产的复用映射（以合同审查为例）

| 现有资产 | 复用方式 |
|---|---|
| RAG（pgvector/SQLite/内存三档 + Obsidian sync） | 合同条款知识库、公司自有合同模板比对 |
| LLM 多 Provider fallback + 成本核算 | 本地 Ollama 省钱 + 云端兜底，单文件合同审查可行 |
| FSM 状态机 | 审查流程：上传 → 解析 → 风险分析 → 人工复核 → 报告 |
| HITL 审批卡片（飞书） | 风险点逐条人工确认/驳回 |
| Guard 策略引擎 | 敏感信息脱敏、禁止输出法律意见的 fail-closed 规则 |
| 审计 WAL + OTel | 审查历史留痕、可追溯 |

需要新做的部分（收敛、不膨胀）：只做 DOCX/PDF 解析（python-docx / pdfplumber）、审查规则模板、一键生成本地报告。

---

## 待验证项（不构成排除理由）

- 中国企业级合同审查 SaaS（如幂律智能等）的真实产品形态和价格，需要进一步一手查证。
- 合同审查质量如何用最小人工验证集（10-20 份真实合同）做护栏，避免"看起来能用"。

---

## 排除记录

| 日期 | 方向 | 排除原因 |
|---|---|---|
| 2026-09-14 | AI Code Review、LLM Gateway/Guard、通用 Agent 工作流、个人 AI 助手、AI 学习助手、内容审批发布、中文 IM 渠道 | 红海 / 重复造轮子 |
| 2026-09-15 | 中文合同审查 | 外部权威数据源不可控；个人开发者无法验证法律质量；错误输出可能造成真实损失；质量无 ground truth |
| 2026-09-15 | 个人账单 / 财务复盘助手 | 账单属高敏感信息，存在泄露与 PIPL 合规风险；同类产品层出不穷 |
| 2026-09-15 | 个人文件 / 档案整理助手 | 证件、发票、合同同样属高敏感信息；同类产品层出不穷 |
| 2026-09-15 | 二手卖家商品助手 | 市场成熟产品多，无差异化 |

**注意**：所有候选方向都被排除，说明"从市场空白出发"的框架本身有问题。下一步转向"从开发者自身真实高频痛点出发"选取方向。
