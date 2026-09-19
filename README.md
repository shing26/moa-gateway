# Agent Gateway

企业级 Agent 网关：智能路由省钱 + 安全守卫/HITL + 评估体系，附 GitHub PR 审查垂直应用。

项目基于 FastAPI 自研状态机编排，默认运行时不依赖 LangChain/LangGraph；LLM 调用层使用 LiteLLM 支持多 Provider、fallback 与成本统计。另提供可选的 LangGraph 第二引擎（`ENGINE=langgraph`）用于框架等价性验证，默认关闭。

## 架构

```mermaid
flowchart LR
  A[飞书 / Webhook] --> B[AuthMiddleware]
  B --> C[Engine FSM]
  C --> D[IntentRouter 三级降级]
  D --> E[Agent 注册表 Coder/General/Review]
  E --> F[LiteLLM Provider + fallback]
  E --> G[工具循环 <=3 轮]
  F --> H[GuardService ALLOW/REVIEW/DENY]
  H --> I[HITL 飞书审批]
  H --> J[Audit WAL / ES]
```

请求链路：飞书/Webhook → 鉴权 → 限流 → FSM → 意图路由（正则 → 微模型 → 路由 LLM）→ Agent 执行 → 静态评估 → 守卫策略 → HITL/拦截 → 渠道适配 → 审计日志。

## 三大卖点

### 1. 智能路由省钱

- 三级降级意图路由：正则命中直接返回，不调 LLM；只有低置信度才升级到微模型/路由模型。
- LiteLLM 内核：`chat` / `chat_with_tools` 接口保持兼容，内置多模型 fallback，网络/429/5xx 自动切换。
- 每次调用记录 `model_used`、`cost_usd`、`llm_latency_ms`、`fallback_used`，随审计日志落盘。
- Eval 实测（`evals/datasets/intent.jsonl`，50 条）：意图准确率 1.0，其中 **46 条（92%）由正则直接命中，0 次 LLM 调用**，只有 4 条落到模型兜底路径。

| 路由层级 | 说明 | 50 条用例命中 |
|---|---|---|
| 正则 | 0 LLM 调用，毫秒级返回 | 46（92%） |
| 微模型 / 路由 LLM | 正则未命中时升级 | 4（8%） |

每次真实调用的成本都会写入审计日志 `cost_usd` 字段，可据此按周聚合实际节省金额。

### 2. 安全守卫 + HITL

- 策略引擎覆盖内网 IP、密钥/Token、价格承诺，输出命中 `deny` 直接拦截，`review` 进入人工审批。
- 策略命中 `review` 的输出强制人工审批，飞书审批卡片闭环：通过送达、拒绝丢弃；`execute_code` 工具未开放，避免占位桩造成假审批。
- 红队实测：200 条对抗用例，期望拦截 120 条全部命中，漏网 0，误拦截 0，召回率/精确率 100%。

运行红队：

```bash
uv run python scripts/redteam/run_redteam.py
```

### 3. 自建 Eval 评估体系

每次改动可以跑三类评估并输出 JSON 报告：

```bash
uv run python evals/run_evals.py --offline
```

| 维度 | 数据量 | 当前指标 |
|---|---|---|
| Intent 意图路由 | 50 条 | 准确率 1.0 |
| Guard 守卫对抗 | 50 条 | deny 召回 1.0 / 精确率 1.0，review 召回 1.0 |
| E2E 端到端 | 30 条 | offline 标记 skipped，CI 不依赖网络 |

红队基线（`scripts/redteam/cases.json`，200 条）：

| 维度 | 数据量 | 当前指标 |
|---|---|---|
| 越狱 / 注入 / 诱导 | 150 条 | 期望拦截 120 条全部命中，漏网 0 |
| 正常对照 | 50 条 | 误拦截 0 |
| 汇总 | 200 条 | 召回率 1.0 / 精确率 1.0 / 误拦截率 0.0 |

报告写入 `evals/reports/latest.json`，包含 `git_sha`、混淆矩阵、拦截指标和 e2e 均分/延迟/成本；intent 准确率低于 0.9 或 guard deny 召回低于 0.95 时退出码非零。

## PR 审查垂直应用

内置 GitHub PR 多 Agent 审查流水线：Triage → 静态分析 → 语义 RAG → 测试覆盖 → 汇总报告。

- Webhook 演示路径：`POST /webhook/github/review`（GitHub webhook body）
- 网关指令：发送 `/review` 后输入 `owner/repo#PR号`
- 无 `GITHUB_TOKEN` 时返回优雅降级文案，不会崩溃

审查结果包含严重级别统计、建议结论和摘要文本，`need_human_review` 时会走守卫/HITL 审计闭环。

## 快速开始

```bash
cd moa-gateway

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

pip install uv
uv sync --all-extras --dev

cp .env.template .env           # 填入真实配置
uv run python scripts/doctor.py  # 检查 Redis / Ollama / pgvector / 端口
.venv\Scripts\python.exe -m app --host 0.0.0.0 --port 8081
```

Windows 上请使用 `python -m app`，它会在 Uvicorn 建 loop 前切换到
`WindowsSelectorEventLoopPolicy`，确保 psycopg 异步 pgvector 连接池可用。

访问：

- 管理后台：<http://localhost:8081/dashboard>
- 自主任务对话：<http://localhost:8081/dashboard/chat>
- 健康检查：<http://localhost:8081/health>
- 依赖级健康检查：<http://localhost:8081/healthz>

8081 是项目推荐的本地端口，用于避开常见的 8080 占用；仍可用 `GATEWAY_PORT` 改端口。

### 本机环境复活

仓库自带环境诊断器，会逐项检查 Python、`.env`、鉴权配置、端口、Redis、
Ollama 主模型、pgvector 和 embedding，并给出可直接执行的修复提示：

```bash
uv run python scripts/doctor.py
uv run python scripts/doctor.py --json
```

使用仓库 Compose 启动持久化依赖：

```bash
docker compose -f docker-compose.dev.yml up -d redis postgres
ollama serve
ollama pull qwen2.5:7b
ollama pull qwen2.5:3b
ollama pull nomic-embed-text:latest
```

Compose 默认将 Redis 映射到 `6380`、pgvector 映射到 `5433`、网关映射到 `8081`，
避免与宿主机已有服务抢占默认端口。`VECTOR_DB_EMBEDDING_DIM` 与
`CODE_REVIEW_EMBEDDING_DIM` 默认按本地 `nomic-embed-text` 的 768 维配置。

## 测试与质量

```bash
# 单元测试
.venv\Scripts\python.exe -m pytest tests/unit -q

# 静态检查
uv run ruff check .

# 安全扫描
uv run bandit -r app -q -s B105,B107,B110,B112,B324

# 评估冒烟
uv run python evals/run_evals.py --offline

# 真实 pgvector + embedding 验收
uv run python scripts/verify_vector_path.py
uv run python scripts/verify_code_review_rag.py

# 长期记忆生命周期验收（跨会话召回 / 冲突覆盖 / 遗忘）
uv run python scripts/verify_long_term_memory.py
```

GitHub Actions CI 会依次执行 pytest、ruff、bandit 和 eval offline；Docker 镜像使用 `python:3.12-slim` + uv 构建。

## 为什么不用 LangChain/LangGraph

核心编排是自研 FSM：状态转移可控、依赖面小、便于学习与面试讲解。多 Agent 网关最需要的是稳定的路由、守卫、审计闭环，而不是再套一层编排框架；LLM 调用层交给 LiteLLM 统一 Provider/fallback/成本即可。

结论不是"没比较过"：仓库里保留了 `app/orchestration/graph.py` 作为**可选的** LangGraph 适配器，把同一条请求链
（route → retrieve → execute → evaluate → guard → HITL → deliver）表达成 `StateGraph`，与 FSM
管道共用同一批协作者对象，并由 `tests/unit/test_langgraph_adapter.py` 做逐字段等价校验。它默认不参与运行时，
依赖也是独立的可选 extra（`uv sync --extra langgraph`），因为解析 LangGraph 会连带拉入 langsmith 等约 20 个包。

**默认运行时仍是 FSM，不替换。** 适配器的价值是"用代码回答标准框架在同约束下能否等价实现"，而不是把控制流
交给框架：FSM 才是这个项目可控性与审计叙事的基础，替换它等于把既有测试、ADR 与守卫/HITL 闭环一起推倒重来。

### 双引擎开关

`ENGINE=langgraph` 时消息路径交给图，四类路径仍回落 FSM：`/` 开头的命令、`RESET`/`CANCEL`、敏感消息挂起、
已有待审批或敏感挂起的会话。两条引擎共用同一个 `SessionStore`（审批）、同一份协作者对象与同一条请求日志
（图路径由 dispatcher 统一落账），因此切换不会静默丢功能。

- 缺 langgraph extra、`ENGINE` 取值未知、或图在运行中抛异常时，均告警并回退 FSM；启动不依赖可选依赖。
- 等价性由 `tests/unit/test_engine_parity_golden.py` 的三个确定性场景（纯回答 / 真实 TaskAgent 工具循环 /
  REVIEW→审批）逐字段比对，与 `test_langgraph_adapter.py` 一起进 CI。
- `evals/run_evals.py --engine fsm|langgraph` 可把同一份 e2e 数据集指向任一引擎；`--offline` 跑的是假引擎，
  不经过真实编排，所以门禁落在上面那组单测。

## 简历条目草稿

> **agent-gateway**（FastAPI · Redis · LiteLLM · OpenTelemetry）
> - 三级降级意图路由 + Provider fallback，微模型分流简单请求，Eval 实测 50 条用例中 46 条正则直出（0 LLM 调用），成本随审计日志逐次核算。
> - 策略守卫 + RBAC + 飞书 HITL 审批闭环，红队 200 条对抗用例召回率/精确率 100%。
> - 自建 Eval 体系（150 条数据集 + LLM-as-judge），每次改动离线回归出 JSON 报告。
> - 后端工程：鉴权中间件、Redis 状态栈/Lua 锁、审计 WAL、OTel 链路、Docker + CI。
> - 支持 FSM / LangGraph 双引擎可切换（`ENGINE`），3 个 golden 场景逐字段等价验证进 CI。
> - 垂直应用：GitHub PR 多 Agent 代码审查（Triage → 静态分析 → 语义 RAG → 测试覆盖 → 报告）。

## 已知边界

- `app/vectordb` 在未配置 `VECTOR_DB_DSN` 时回退到中文 bigram 关键词检索；配置
  pgvector + embedding 后切换到混合向量检索，并可通过 `/healthz` 观察后端状态。
- Redis 不可用时回退内存存储，内存回退也支持幂等锁脚本（acquire/release/extend + TTL），但只保证单进程内语义。
- 限流器为内存滑窗实现，适合单机开发；多实例需换 Redis 限流。
- `app/memory.py` 的 `_SyncBridge` 同步桥是已知技术债：同步线程跑 asyncio loop，测试与运行时都不依赖它做时序保证。
- 双引擎模式下的边界：LangGraph 是可选 extra，Docker 镜像默认不带（`ENGINE=langgraph` 会自动回退 FSM 并告警）；
  图的 checkpoint 是进程内的 `InMemorySaver`，与 FSM 的 `_session_states` 同级，都不承诺跨重启恢复。
- FSM 会话状态保存在进程内（`Engine._session_states`）；`app/redis_state/stack.py` 与 `lock.py` 的 Redis 状态栈 / Lua
  锁目前只在单测中被调用，**未接入请求路径**，多实例部署前需要先接线。
- `app/main.py` 使用 FastAPI lifespan 管理启动/关闭钩子（`on_event` 已迁移）。
- 8 条飞书/HITL 集成用例仍会在没有真实测试应用凭据时条件跳过；它们不是失败。
- 所有密钥通过环境变量注入，`.env`、`logs/`、`data/`、`evals/reports/` 不入库。
