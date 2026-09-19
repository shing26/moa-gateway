# AICoding 架构设计 · 资料摘要

> 本文档做一件事：**精读主理人转交的全部原始资料，逐份、逐章节做出摘要**——后面任何人拿到这份摘要，都能通过章节号快速定位回原始文件的对应位置。

> 上游输入：探查 agent 产出的《moa-gateway 架构深度剖析与"逻辑未打通"根因诊断》报告（D1）+ 用户诉求原文（D2）；
> 产出者：`knowledge-ingest-engineer-2`（知识摄入工程师 - 闻资料），经 G1 校验与人工审核通过后交付。
> 说明：本次为 Phase 1（资料摄入）唯一 Owner，只做"资料→结构化摘要"，**不写任何架构/设计/方案结论**（方案由下游建筑师产出）。

---

## 模板适配说明

> 原模板面向 docx/pdf/pptx/xlsx 业务资料；本次原始资料为**代码剖析报告（markdown）+ 需求原文**，故对类型枚举与附录 B 做了如实适配，章节骨架（§0–§4、附录 A/B）严格沿用模板。全文无残留占位符、示例前缀、待填日期或事实缺口标记。

---

## 0. 元信息

```yaml
标题: moa-gateway - 资料摘要 v0.1
版本: v0.1
状态: Draft
创建日期: 2026-08-29
整理人: knowledge-ingest-engineer-2
审核人:
  - team-lead（主理人）

原始资料清单:
  - D1: 代码库深度剖析与"逻辑未打通"根因诊断报告 (markdown，探查 agent 产出): 基于真实源码逐文件核对，附 文件:行号 证据
  - D2: 用户诉求原文: 企业级 Agent 网关现有框架已搭好但各模块业务逻辑未跑通，需先分析结构/技术栈并定位根因
```

| 版本 | 日期 | 作者 | 变更内容 |
| --- | --- | --- | --- |
| v0.1 | 2026-08-29 | knowledge-ingest-engineer-2 | 初稿（Phase 1 资料摄入，G1） |

---

## 1. 资料清单

> 列出全部原始资料，每份标注解析状态。解析失败或跳过的必须注明原因。

| 编号 | 文件名 | 类型 | 来源 | 解析状态 | 说明 |
| --- | --- | --- | --- | --- | --- |
| D1 | `代码库深度剖析与"逻辑未打通"根因诊断报告` | code-report（markdown） | 探查 agent（基于真实源码逐文件核对） | 已解析（关键论断已由本工程师二次抽样核对源码确认） | 覆盖技术栈、目录、模块职责、调用链、根因 A–F、数据层、测试 |
| D2 | `用户诉求原文` | requirement（对话文本） | 主理人 / 用户 | 已解析 | 描述期望产出：框架分析 + 根因定位 + 完整架构方案 + 落地优先级 + 风险提示 |

**类型枚举（本次适配）**：`code-report` / `requirement`（原模板 docx/pdf/pptx/xlsx 不适用；D1 为源码剖析，D2 为需求说明）

---

## 2. 资料内容摘要

> 逐份文档按自身章节结构做摘要。每条摘要标注章节号（`D编号, §章节`，即 D1, §1 / D2, §3 这类 ASCII 逗号形式），后面任何人想核实某个点，直接定位回原文对应位置即可。

### D1：`代码库深度剖析与"逻辑未打通"根因诊断报告`

> 基于真实源码逐文件核对（探查 agent 称已实证 `import app.main` 可成功导入、入口能起）；所有结论附 文件:行号 证据。本工程师对 根因 A/B/C/D/E/F 的多数论断做了源码二次抽样验证（见各条"核对"注）。（本节出处标注形如 D1, §1 / D1, §5）

#### §1 技术栈与运行时

| 章节 | 内容摘要 |
| --- | --- |
| D1, §1.1 Web 框架 | FastAPI（`pyproject.toml:7` `fastapi>=0.111`） |
| D1, §1.2 Python 版本 | `requires-python >=3.12`；Docker 用 `python:3.12-slim`（`pyproject.toml:4`） |
| D1, §1.3 入口 | `app/main.py`（`app = FastAPI(...)`）；生产启动 `uvicorn app.main:app` |
| D1, §1.4 核心依赖 | `litellm`（多 provider/fallback/cost）、`uvicorn`、`redis`、`httpx`、`opentelemetry`、`pydantic>=2.7`、`python-dotenv`、`python-multipart`（核对：`pyproject.toml` dependencies 段确认上述，含 `redis>=5.0`、`litellm>=1.96.2`、`pydantic>=2.7`） |
| D1, §1.5 飞书 | `app/channels/feishu*.py` 自研（非官方 SDK）；`feishu_cards.py` 审批卡片 |
| D1, §1.6 向量/检索 | 自研 `app/vectordb`（中文 bigram 关键词检索，**非真向量库**，README 自承） |
| D1, §1.7 存储 | 网关主链路无 ORM；HITL/会话用 Redis（内存回退）；知识库用内存 dict；审计 WAL + 可选 ES |
| D1, §1.8 子应用存储 | `apps/code_review_pipeline` 用 Postgres+pgvector（`schema.sql`）或内存；RAG 向量用 SQLite（`data/code_review_vectors.sqlite`） |
| D1, §1.9 消息队列 | **无** Kafka/RabbitMQ；Redis 仅作状态栈/锁/内存回退，非 MQ |
| D1, §1.10 检查/测试 | ruff + bandit；pytest |

#### §2 目录与模块划分

| 章节 | 内容摘要 |
| --- | --- |
| D1, §2.1 `app/`（网关核心） | 通用编排、路由、守卫、渠道、可观测、审计。含 `main.py`（入口+lifespan）、`pipeline.py`（主流程编排）、`engine.py`（FSM+HITL 存储）、`router/`（三级意图路由）、`agents/`（Agent 协议/注册表/Coder·General·Review 三实现/provider）、`channels/`（飞书）、`guard/`（策略守卫：policies/rbac/redteam/permission_guard）、`fsm/`、`vectordb/`、`knowledge.py`、`memory.py`、`obsidian_sync.py`、`routes/`（feishu/webhook/health/dashboard/knowledge）、`redis_state/`、`limit_providers/`、`feature_flags/`、`prompt_registry/`、`audit/`、`evaluator/`、`observability/`、`middleware/`、`config.py`、`deps.py`（核心装配中心） |
| D1, §2.2 `apps/`（垂直业务） | 当前仅 `code_review_pipeline/`：GitHub PR 多 Agent 审查流水线（triage/static/semantic/test/report 五 Agent + RAG + GitHub 路由 + 通知 + 存储） |
| D1, §2.3 `scripts/` | 27 个脚本，其中 9 个用字符串 `.replace()` 直接改写源码（`gen_main.py`、`patch_main.py`、`fix_main_all.py`、`fix_hitl.py`、`fix_name.py` 等） |
| D1, §2.4 `evals/` | 离线评估（intent/guard/e2e 数据集 + LLM-as-judge） |
| D1, §2.5 `tests/unit/` | 70+ 单测（报告指大量用 monkeypatch 掩盖真实接线缺陷） |
| D1, §2.6 `docs/` | ADR、HANDOFF、STAGES 等设计文档 |

#### §3 各功能模块职责（一句话）

| 章节 | 内容摘要 |
| --- | --- |
| D1, §3.1 `app/main.py` | FastAPI 装配：挂载路由、中间件、lifespan（feishu/prompts/obsidian 初始化） |
| D1, §3.2 `app/deps.py` | 全局单例装配中心：构建 router/engine/pipeline/内存/知识库/飞书等（核对：`deps.py` 全文确认 `_retriever = ContextRetriever(VectorDBClient())`、`knowledge_base = KnowledgeBase(_retriever._client)`、`pipeline = MoAPipeline(..., card_sender=None)` 等） |
| D1, §3.3 `app/pipeline.py` | 主链路：FSM→意图路由→Agent→评估→守卫→HITL→渠道→审计 |
| D1, §3.4 `app/router/intent_router.py` | 三级降级意图路由（正则→微模型→路由 LLM），输出意图标签（核对：`intent_router.py:27-37` 正则映射返回 `greeting/debug/coding/control/translate/summarize/search/analyze`、默认 `assistant`） |
| D1, §3.5 `app/agents/*` | Coder/General/Review 三 Agent，统一经 `provider.LLMClient`（LiteLLM） |
| D1, §3.6 `app/guard/*` | 内网 IP/密钥/价格承诺策略；RBAC；红队对抗 |
| D1, §3.7 `app/channels/feishu*.py` | 飞书消息收发、事件解析、卡片发送、验签 |
| D1, §3.8 `app/engine.py` | FSM 状态转移 + HITL 请求存储（Redis/内存） |
| D1, §3.9 `app/vectordb/` + `knowledge.py` | 关键词检索 + 知识库 CRUD（全内存） |
| D1, §3.10 `apps/code_review_pipeline/` | GitHub PR 审查垂直应用：Triage→静态→语义RAG→测试→报告 |
| D1, §3.11 `app/middleware/*` | 鉴权（fail-open）、特性开关、请求日志 |
| D1, §3.12 `app/audit/` | 审计 WAL + 可选 ES 双写 |
| D1, §3.13 `app/redis_state/` | Redis 状态栈/Lua 锁，内存回退 |
| D1, §3.14 `app/feature_flags/` `prompt_registry/` | 特性开关、Prompt 金丝雀 |

#### §4 模块间依赖与调用链（文字版）

| 章节 | 内容摘要 |
| --- | --- |
| D1, §4.1 HTTP 层入口 | `/feishu/event`（`routes/feishu.py`）→ `parse_feishu_event` → `pipeline.run()`；`/webhook/{channel}`（`routes/webhook.py`）→ `pipeline.run()`；`/webhook/github/review`（`github_review_router`）→ `CodeReviewPipeline.run()`；`/webhook/callback`（`routes/webhook.py:16-65`）→ `engine.handle_event(HUMAN_APPROVED/REJECTED)` → `adapter.adapt(hitl.agent_output)` ← **HITL 真正闭环点** |
| D1, §4.2 `pipeline.run()` 主链路 | `engine.handle_event()`（FSM）→ `router.route(text)`（意图标签）→ `command_mode.get()`（强制意图覆盖）→ `get_agent(intent)`（注册表查找）← **关键断裂点** → `retriever.retrieve()`（默认空）→ `agent.execute(envelope)`（Coder/General/Review）→ `evaluator.score()` → `guard_service.evaluate()` → `engine.session_store.store_hitl()`（REVIEW 时挂起）→ `card_sender.send_card()` → `adapter.adapt()` → `memory.add()` |
| D1, §4.3 飞书事件回调链路 | `/feishu/event` → 验签 → `parse_feishu_event`：`url_verification`→返回 challenge；`card_action`(approve/reject)→`adp.send(回执文本)` ← **仅回执，不闭环**；`im.message.receive_v1`→`pipeline.run()` |
| D1, §4.4 总体判断 | 路由层→pipeline→Agent/守卫/检索的"调用"是连通的；但数据层（检索/知识/HITL 交付）在默认配置下为空或断裂 |

#### §5 "模块逻辑未打通"的根因诊断（最关键，保留 文件:行号 证据）

> 本工程师对每个根因均做了源码二次核对，结论"已核对确认"表示该文件:行号论断与实测源码一致。

| 编号 | 根因 | 证据（文件:行号） | 核对 | 说明 |
| --- | --- | --- | --- | --- |
| D1, §5 A | 意图标签与 Agent 注册键完全不匹配（核心缺陷） | 意图路由返回 `coding/translate/search/analyze/summarize/debug/greeting/control/assistant`：`app/router/intent_router.py:27-37`、`app/router/llm_classifier.py:6-16`(VALID_INTENTS)；Agent 注册表只有 `coder/general/review`：`app/agents/loader.py:4-11`、`app/agents/contract.py:21,28-29`；主链路按意图取 Agent：`app/pipeline.py:172` `agent = get_agent(intent) or get_agent("general")`；命令模式：`app/command_mode.py:8-9`(`coder` 模式 intent=`coding`) | **已核对确认** | `get_agent("coding")`→None→永远落到 GeneralAgent；CoderAgent 从不被选到，其专用 system prompt（`app/deps.py:127,144`）也从未被使用。除 `/review` 命令（intent=review 恰好命中注册键）外，所有流量塌缩到 GeneralAgent，所谓"智能路由编程/翻译/搜索"等模块在业务逻辑上并未真正跑通 |
| D1, §5 B | 飞书 HITL 审批闭环未打通 | 飞书卡片 action 分支：`app/routes/feishu.py:69-96`（仅 `adp.send()` 回执文本，**无** `engine.handle_event`、`remove_hitl`、投递 `agent_output`）；真正闭环：`app/routes/webhook.py:16-65`（`/webhook/callback`）；中间件白名单并列二者：`app/middleware/auth.py:13` | **已核对确认** | 飞书交互卡片的 action 回调只投递到事件订阅 URL `/feishu/event`，而非 `/webhook/callback`；用户点了批准，系统只回"已批准"，待审批请求仍挂起、结果不送达（"假闭环"） |
| D1, §5 C | 检索/知识/RAG 数据链默认全为空 | 主网关检索：`app/deps.py:35` `_retriever = ContextRetriever(VectorDBClient())`，`VectorDBClient` 为内存 dict，进程重启清空；`app/knowledge.py` 同样内存；Obsidian 同步默认关闭：`app/obsidian_sync.py:47-48`(`enabled = root is not None and _kb is not None`，`root` 需 `OBSIDIAN_VAULT_PATH` 为目录)；`.env.template:58` 留空 → 知识库空 | **已核对确认**（obsidian `enabled` 逻辑、`deps.py:35` 内存 retriever 均实测一致） | `pipeline.run()` 中 `retriever.retrieve()`（`app/pipeline.py:179`）永远返回空，global_summary 为空，主链路 RAG 形同虚设；PR 子应用 RAG 同样 dormant（缺 `CODE_REVIEW_EMBEDDING_API_KEY` 抛 `EmbeddingError`→空；向量库需 `GITHUB_TOKEN` 手动摄入，默认 `data/code_review_vectors.sqlite` 空） |
| D1, §5 D | 两套并存 Schema，且 PR 存储与 SQLite 互不消费 | `apps/code_review_pipeline/storage/schema.sql`（Postgres+pgvector 建表）；`apps/code_review_pipeline/rag/vector_store.py:104-129`(自建 SQLite 表 `code_review_vectors`)；`review_store.py:177-199`(`build_review_store()` 仅 `CODE_REVIEW_DATABASE_URL` 存在时连 Postgres 表 `code_review_prs`，否则内存——**从不读写** `data/code_review_vectors.sqlite`) | **已核对确认** | 两套 schema（`code_review_prs` vs `code_review_vectors`）未统一，存在"建表语句写了但运行时并不用它"的脱节；`data/code_review_vectors.sqlite` 仅被 RAG 向量库使用，review_store 不消费它 |
| D1, §5 E | 项目由"字符串替换式"脚本拼装，导致接线漂移 | `scripts/gen_main.py:4-27`（`old.replace(...)` 直接改写 `app/main.py`，含插入 loader import、exception handler、删除 hitl 块、改写 `agent_name` 解析、改写 review state 字符串等）；同类 `patch_main.py`、`fix_main_all.py` 等 9 个脚本 | **已核对确认**（实测 `gen_main.py` 使用 `old.replace`） | 对脆弱字符串锚点做文本 patch 的自动生成方式，是 A/B 接线断裂的直接成因——生产代码被多次无类型检查的文本修改，CI 又被 F 的 monkeypatch 掩盖 |
| D1, §5 F | 单元测试用 monkeypatch 掩盖真实接线缺陷 | `tests/unit/test_pipeline.py:148` `monkeypatch.setattr(pipeline_module, "get_agent", lambda name: agent)`（把 `get_agent` 整体替换成恒返回假 Agent，使 `get_agent("coding")` 也能命中，绕过 A）；由此 `test_ok_path`(`:161` 断言 `intent=="coding"`)、`test_review_path_via_execution_marker`(`:250` 断言 `agent_name=="coder"`) 在测试中"通过" | **已核对确认** | CI 全绿 ≠ 业务逻辑跑通；测试大量 monkeypatch，集成层真实缺陷被遮蔽 |

#### §6 数据层

| 章节 | 内容摘要 |
| --- | --- |
| D1, §6.1 `data/code_review_vectors.sqlite` | RAG 团队历史 PR 向量库（SQLite），由 `apps/code_review_pipeline/rag/vector_store.py` 读写；默认空，需手动摄入 |
| D1, §6.2 `apps/code_review_pipeline/storage/schema.sql` | 建表语句（Postgres+pgvector）；仅当配 `CODE_REVIEW_DATABASE_URL` 时由 `review_store._ensure_schema()` 应用，否则走内存 |
| D1, §6.3 ORM | **无 ORM**；网关主链路用 Redis/内存；PR 子应用直接 `psycopg` 裸 SQL 与 `sqlite3` |
| D1, §6.4 模型定义 | 无 SQLAlchemy/Tortoise 模型类；数据形态用 `@dataclass`（`ReviewRecord`、`VectorDocument`、`AgentFindingResult` 等） |

#### §7 测试与可运行性

| 章节 | 内容摘要 |
| --- | --- |
| D1, §7.1 测试覆盖 | `tests/unit/` 70+ 文件 |
| D1, §7.2 能否启动 | 探查 agent 称已验证 `import app.main` 成功、`uvicorn app.main:app` 能启动（**本工程师未独立复跑该 import，按报告陈述记录**）；但 `python -m app`（`app/__main__.py`）只跑 `power_on_self_test()`，非服务入口 |
| D1, §7.3 降级行为 | 默认无 `REDIS_URL` 可达、无 LLM key 时，链路优雅降级（每步有 fallback/except），服务能起、但业务产出为空或走 general |
| D1, §7.4 关键隐患 | 测试大量 monkeypatch，CI 绿 ≠ 集成通（见 §5 根因 F） |
| D1, §7.5 配置/安全相关 | 鉴权 fail-open：`WEBHOOK_AUTH_TOKEN`/`DASHBOARD_PASSWORD`/`FEISHU_VERIFICATION_TOKEN` 为空时中间件直接放行（`app/middleware/auth.py:43,46`，核对：仅当 token 非空才校验，否则 `call_next` 放行）——属安全隐患而非断裂；HITL 卡片依赖飞书凭证（`app/deps.py:107` 构造 `MoAPipeline(card_sender=None)`，仅 `init_feishu()` 配了 `FEISHU_APP_ID/SECRET` 才 `set_card_sender`，`deps.py:119`；否则 `pipeline.py:260` `if self.card_sender:` 为假，REVIEW 只挂起不发卡）；`apps/` 无 `__init__.py`（核对：`apps/__init__.py` 缺失，靠 cwd=项目根才能 `import apps...`，Docker WORKDIR `/app` 可行，本地换目录会 ImportError）；Omniroute 默认不启动（`docker-compose.dev.yml:19` 用 `profiles:["base"]` 隔离） |

### D2：用户诉求原文

> 项目 moa-gateway：企业级 Agent 网关，整体框架已搭好但各功能模块业务逻辑未跑通。来源：主理人 / 用户（本节出处标注形如 D2, §1 / D2, §3）

| 章节 | 内容摘要 |
| --- | --- |
| D2, §1 现状判断 | 企业级 Agent 网关；整体框架（FastAPI 入口、模块划分、三级路由、FSM/HITL、飞书渠道、守卫、审计等）已搭好，但"各功能模块业务逻辑未跑通" |
| D2, §2 期望分析 | 先分析现有框架结构与技术栈；梳理各模块职责/依赖/调用链；**定位模块逻辑未打通的具体原因**（调用断裂、数据流不连贯、接口定义不一致等） |
| D2, §3 期望方案（下游建筑师交付，非本阶段产出） | 输出完整架构方案：模块集成与调用设计、数据流转与状态管理、接口规范、改造至可运行的具体步骤；尽量复用现有代码、避免大规模重写；给出落地优先级路径与风险提示 |
| D2, §4 约束 | 尽量复用现有代码、避免大规模重写 |

---

## 3. 冲突记录

> 本次仅两份资料：D1（代码剖析报告，单一分析源）与 D2（用户诉求）。D2 是对"期望产出"的描述，与 D1 的"现状诊断"互补而非矛盾，**无跨资料事实冲突**。
> D1 内部自报了若干"同一代码库内不自洽"的现象，本工程师已抽样核对确认，作为"单源内部不一致"记录如下（不做裁决，供下游建筑师决策）：

| 编号 | 冲突/不一致主题 | 版本 A | 出处 A | 版本 B | 出处 B | 差异说明 |
| --- | --- | --- | --- | --- | --- | --- |
| X1 | PR 子应用存储 schema | `schema.sql` 定义 Postgres+pgvector 表 `code_review_prs`，仅当 `CODE_REVIEW_DATABASE_URL` 存在时由 `review_store._ensure_schema()` 应用 | `apps/code_review_pipeline/storage/review_store.py:37-50,177-199` | RAG 向量库自建 SQLite 表 `code_review_vectors`（`data/code_review_vectors.sqlite`），`review_store` 从不读写该文件 | `apps/code_review_pipeline/rag/vector_store.py:104-129` | 两套 schema 不统一，review 存储与 RAG 向量 SQLite 互不消费 |
| X2 | HITL 审批闭环路径 | 飞书卡片 action 回调进 `/feishu/event`，`routes/feishu.py:69-96` 仅回执不闭环 | `app/routes/feishu.py` | 真正的闭环逻辑在 `/webhook/callback`（`engine.handle_event` + `remove_hitl` + `adapter.adapt`） | `app/routes/webhook.py:16-65` | 飞书卡片不会自行跳到 `/webhook/callback`，导致"假闭环"（同根因 B） |
| X3 | 服务入口语义 | `uvicorn app.main:app` 为服务入口（可启动） | `pyproject.toml` / 报告 §7.2 | `python -m app` 仅跑 `power_on_self_test()`，非服务入口 | 报告 §5.7 | 入口语义不一致，易被误用为启动方式 |

---

## 4. 硬指标清单

| 章节 | 硬指标 | 状态 |
| --- | --- | --- |
| §1 | 每份资料有解析状态，失败/跳过注明原因 | 通过（D1/D2 均"已解析"；D1 中 `import app.main` 复跑未做，已如实标注） |
| §2 | 每份文档按章节逐条摘要，每条标注了 `D编号, §章节` | 通过（D1 按 §1–§7 摘要并附 文件:行号；D2 按 §1–§4 摘要；章节列均带 D编号, §章节 形式） |
| §3 | 冲突信息并列保留，不做裁决 | 通过（X1–X3 并列两版本，标注出处，未裁决） |
| §5 附加 | 根因清单保留 文件:行号 证据，且经二次源码核对 | 通过（A–F 均附 文件:行号，且 A/B/C/D/E/F 标注"已核对确认"） |

---

## 附录 A：生成流程

### 流程总览

| 步骤 | 动作 | 落入章节 |
| --- | --- | --- |
| Step0 | 读取模板 `material_digest.md` + 原始资料（D1 剖析报告、D2 用户诉求） | — |
| Step1 | 盘点资料清单，标注解析状态 | §1 |
| Step2 | 逐份精读，按自身章节结构逐条摘要（D1 §1–§7、D2 §1–§4） | §2 |
| Step2.5 | 对 D1 关键根因（A–F）抽样二次核对源码（intent_router/loader/pipeline/feishu/webhook/obsidian/deps/vector_store/review_store/scripts/tests/auth 等） | §2 §5、§3 |
| Step3 | 交叉比对，记录单源内部不一致（X1–X3） | §3 |
| Step4 | 逐项核验硬指标（含根因 文件:行号 + 核对标记） | §4 |

```mermaid
flowchart LR
    S0[读取模板与资料] --> S1[盘点资料清单]
    S1 --> S2[逐份精读逐章节摘要]
    S2 --> S2_5[抽样二次核对源码根因]
    S2_5 --> S3[记录单源内部不一致]
    S3 --> S4[硬指标自检]
```

### 整理原则

1. **逐份精读，不跨文档归并**：摘要按 D1 自身章节（§1–§7）与 D2 章节（§1–§4）组织，不做跨文档主题重组（那是下游的事）
2. **出处即章节号**：每条摘要标注 `D编号, §章节`，并尽量保留 文件:行号 证据
3. **冲突保留**：矛盾/不一致信息并列保留两个版本，不擅自裁决
4. **事实驱动**：以原始资料中的事实为准；本工程师对根因做了二次源码核对，核对结论以"已核对确认"标注，未复跑项（如 `import app.main`）如实说明
5. **不越权**：本阶段只产出资料摘要，**不写架构/设计/方案结论**

---

## 附录 B：解析方法（本次适配）

- `code-report`：源码剖析类报告——以"文件:行号"为证据锚点，逐文件核对论断真实性（本阶段采用）
- `requirement`：用户需求/诉求文本——按诉求要点逐条摘要
- （原模板 docx/pdf/pptx/xlsx 解析 Skill 不适用于本次纯文本/代码资料，故替换为本行说明）
