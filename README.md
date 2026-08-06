# MoA Engine Gateway

多智能体（Multi-Agent）动态路由 + 状态机网关，面向飞书等 IM 渠道提供消息接入、意图路由、Agent 执行、安全守卫、知识库检索与 Web 管理后台。

> 当前版本：v0.1 MVP（本地开发 / 单机部署）

## 功能特性

- **多渠道接入**：通用 `/webhook/{channel}` 端点 + 飞书事件回调（`/feishu/event`），支持 HITL 审批卡片
- **意图路由与指令**：`/coding`、`/translate`、`/search`、`/analyze`、`/default` 模式切换
- **状态机引擎**：基于 ADR 转移矩阵的 FSM，管理消息接收、路由、执行、审批等状态
- **真实 Sub-Agent**：Coder / General Agent，通过 OpenAI 兼容协议调用 DeepSeek、NVIDIA NIM、OmniRoute 等 Provider
- **安全守卫**：Evaluator 评分 + GuardService（ALLOW / REVIEW / DENY），支持人工审批（HITL）
- **知识库 / RAG**：文档分块、关键词检索（支持中文双字匹配）、Obsidian Vault 自动同步
- **可观测性**：OpenTelemetry 链路追踪、审计日志（JSONL WAL）、请求日志实时展示
- **管理后台**：健康状态、活跃会话、知识库管理、Webhook 测试、请求日志、运行配置
- **限流与幂等**：Redis 状态栈、Lua 幂等锁、分布式限流（开发环境可降级内存实现）
- **工具调用（Function Calling）**：内置 `knowledge_search` / `current_time` / `execute_code` 工具注册表，模型可自主选择调用，支持多轮工具执行循环
- **HITL 审批真实链路**：`execute_code` 等高危操作返回审批标记，强制走人工审批并触发飞书审批卡片，审批通过后输出才送达
- **Webhook / Dashboard 鉴权**：`WEBHOOK_AUTH_TOKEN`（请求头校验）+ `DASHBOARD_PASSWORD`（Basic Auth），未配置时自动关闭（fail-open）
- **配置接真实环境变量**：基于 python-dotenv 从 `.env` 加载全部配置，密钥不写入代码
- **会话 / HITL Redis 持久化**：会话记忆与待审批请求持久化到 Redis（带 TTL），Redis 不可用时自动回退内存实现

## 目录结构

```text
app/
  agents/            Sub-Agent 注册与 LLM Provider（tools.py 工具注册表）
  audit/             审计模型与 JSONL WAL
  channels/          飞书渠道适配（消息 / 事件 / 卡片 / Token）
  evaluator/         输出评估器
  fsm/               状态机
  guard/             权限与守卫服务
  knowledge.py       知识库分块与管理
  memory.py          会话记忆（Redis 持久化，含内存回退）
  middleware/        请求日志、Feature Flag、auth.py 鉴权
  obsidian_sync.py   Obsidian Vault 同步
  redis_state/       Redis 状态栈与幂等锁
  routes/            Webhook / 飞书 / 健康检查 / 知识库 / Dashboard
  vectordb/          内存向量库与知识检索
tests/unit/          单元测试
scripts/             开发辅助脚本
docs/                架构与决策文档
```

## 快速开始

### 方式一：本地运行（推荐开发）

```bash
git clone <your-repo-url>
cd moa-gateway

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

pip install uv
uv sync --all-extras --dev

cp .env.template .env          # 填入真实配置
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 方式二：Docker Compose

```bash
docker compose -f docker-compose.dev.yml up --build
```

启动后访问：

- 管理后台：<http://localhost:8080/dashboard>
- 健康检查：<http://localhost:8080/health>
- 依赖级健康检查：<http://localhost:8080/healthz>

## 环境变量

复制 `.env.template` 为 `.env` 并填写。`.env` 已加入 `.gitignore`，请勿提交真实密钥。

```dotenv
# LLM Provider（OpenAI 兼容）
OPENAI_API_KEY=sk-your-api-key
OPENAI_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# Webhook / Dashboard 鉴权
# 留空则对应鉴权不启用（fail-open）；生产环境必须配置
WEBHOOK_AUTH_TOKEN=
DASHBOARD_PASSWORD=

# Redis（默认 localhost:6379，生产可配 Sentinel）
REDIS_URL=redis://localhost:6379/0
# 主 Redis 不可用时回退内存实现（1/true 开启，默认开启）
REDIS_ENABLE_FALLBACK=true
# REDIS_SENTINEL_HOSTS=host1:26379,host2:26379,host3:26379
# REDIS_SENTINEL_MASTER=mymaster

# 飞书（可选，HITL 审批卡片需要）
# FEISHU_APP_ID=cli_xxx
# FEISHU_APP_SECRET=xxx

# 路由与微模型超时（毫秒）
ROUTER_LLM_TIMEOUT_MS=2000
MICRO_LLM_TIMEOUT_MS=1000

# 人工审批 HITL（1/true 开启，默认关闭）
HITL_ENABLED=false

# 运行环境
MOA_ENV=dev

# 审计日志
LOG_DIR=./logs/
LOG_RETENTION_DAYS=90

# Obsidian 知识库（可选，空值关闭同步）
OBSIDIAN_VAULT_PATH=
OBSIDIAN_SYNC_FOLDER=

# OmniRoute 等聚合网关（可选）
# OPENAI_BASE_URL=http://localhost:20128/v1
# LLM_MODEL=qwen-web/qwen3.7-plus
```

### Provider 示例

任何 OpenAI 兼容的 Chat Completions 接口都可以通过 `OPENAI_BASE_URL` 接入：

```dotenv
# DeepSeek
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

```dotenv
# NVIDIA NIM
OPENAI_API_KEY=nvapi-xxx
OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1
LLM_MODEL=nvidia/nemotron-3-ultra-550b-a55b
```

```dotenv
# OmniRoute 聚合网关
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=http://localhost:20128/v1
LLM_MODEL=qwen-web/qwen3.7-plus
```

## 飞书接入

1. 在飞书开放平台创建应用，开启机器人能力
2. 配置事件订阅为 **将事件发送至开发者服务器**，回调地址填你的公网地址（本地可用 Cloudflare Tunnel）：
   - 事件订阅 URL：`https://<your-domain>/feishu/event`
3. 在“事件与回调”中添加 `im.message.receive_v1` 消息事件
4. 在 `.env` 中配置：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

消息以 `/` 开头的指令会切换模式，普通消息进入 MoA 管线。

## 工具与 HITL 流程

Sub-Agent 支持 OpenAI 兼容的 Function Calling：网关把工具注册表（`app/agents/tools.py`）中的 Schema 随请求一起发送给模型，模型需要时返回 `tool_calls`，网关执行对应工具并把结果回填为 `tool` 消息后继续请求模型（最多 3 轮循环）。

内置工具：

- `knowledge_search`：检索知识库，返回相关文档片段
- `current_time`：返回当前本地时间（ISO 8601 格式）
- `execute_code`：执行一段代码（高危操作）——**不会真正执行**，而是返回 `EXECUTION_REQUIRES_APPROVAL` 审批标记

当输出包含审批标记时，守卫强制走 REVIEW 分支：请求进入挂起状态（`SUSPENDED` / `pending_review`），待审批请求持久化到 Redis（`moa:hitl:*`，1 小时 TTL）；若已配置飞书，则向用户发送人工审批卡片（含通过 / 拒绝按钮），审批通过后输出才会送达渠道，拒绝则丢弃输出。

## 知识库 / Obsidian

设置 `OBSIDIAN_VAULT_PATH` 指向本地 Vault 后，网关启动时会自动同步其中的 Markdown 文档并分块入库。`/search` 模式下会注入知识库检索结果，模型基于库内资料回答。

也可以在 Dashboard 的“知识库”页面上传文档、测试检索。

## 管理后台

Dashboard 提供：

- 概览：服务健康、Redis 状态、活跃会话、最近日志
- 会话：查看 / 清空会话、切换模式
- 知识库：上传、删除、检索测试
- 测试：快速模拟 Webhook 请求
- 日志：实时刷新请求审计日志（含输入 / 输出摘要、状态码、耗时）
- 运维：查看运行配置与 Feature Flags

## 测试

```bash
pytest tests/unit -q
```

## 安全合规基线

Guard 策略引擎 + 可审计 HITL 审批闭环，覆盖输出内容检测、审计落盘与可视化监控。

### 策略引擎

内置 3 条策略（`app/guard/policies.py`，单例 `policy_engine`）：

| ID | 名称 | 严重级 | 说明 |
| --- | --- | --- | --- |
| `policy.security.internal_ip` | 内网 IP 泄露 | deny | 检测内网、回环、链路本地及运营商 NAT 地址（10.x / 172.16-31.x / 192.168.x / 127.x / 169.254.x / 100.64.x） |
| `policy.security.secret_leak` | 密钥泄露 | deny | 检测 OpenAI 风格密钥、AWS Access Key、GitHub Token、私钥块及 API Key 赋值 |
| `policy.compliance.no_price_commitment` | 价格承诺 | review | 检测价格承诺表述（金额 + 单位），命中后需人工复核 |

严重级：`deny` 直接拦截；`review` 进入人工审批。管线按 **行为 DENY > 输出 DENY > 行为 REVIEW > 输出 REVIEW > ALLOW** 合并守卫结果，命中策略 ID 会写入 `PipelineResult.policy_hits` 并进入审计。

自定义策略：继承 `Policy`、实现 `detect()` 返回 `PolicyHit` 列表，再 `policy_engine.register()` 注册（同 ID 自动去重）：

```python
from app.guard.policies import Policy, PolicyHit, policy_engine

class PiiPhonePolicy(Policy):
    def __init__(self) -> None:
        super().__init__(
            policy_id="policy.security.pii_phone",
            name="手机号泄露",
            description="检测 11 位手机号泄露",
            severity="deny",
        )

    def detect(self, text: str) -> list[PolicyHit]:
        return [PolicyHit(self.policy_id, self.severity, m.group(0))
                for m in re.finditer(r"\b1[3-9]\d{9}\b", text)]

policy_engine.register(PiiPhonePolicy())
```

### 审计链路

- **双写**：每次请求经 `log_request` 写入 JSONL WAL（默认 `logs/audit-YYYY-MM-DD.jsonl`，90 天保留），可选配置 Elasticsearch 后由 `EsWriter` 批量写入，ES 不可用时自动回退 WAL
- **ES 配置**：`ES_HOSTS`（逗号分隔，如 `http://es1:9200,http://es2:9200`）+ `ES_INDEX_PREFIX`（默认 `moa-audit`），留空则关闭 ES 通道
- **审计字段**（`AuditEntry`）：`trace_id` / `session_id` / `agent_name` / `intent` / `guard_action` / `guard_reason` / `eval_score` / `eval_issues`，安全合规改造新增 `policy_hits`（命中策略 ID 列表）、`violation`（首个命中策略）、`hitl_decision`（approve / reject）、`hitl_duration_ms`（审批耗时，毫秒）

### Dashboard 安全合规页

访问 `/dashboard/security`：

- **拦截趋势**：近 7 天 `deny` / `review` 每日次数（表格 + 柱状图）
- **高风险会话 Top10**：按 deny / review 拦截次数聚合，展示最近一次违规策略
- **人工审批耗时分布**：审批样本数、P50 / P95 / 最大耗时，并按 0-30s / 30-120s / 120s+ 分桶
- **策略清单**：服务端注册策略列表（ID / 名称 / 严重度 / 说明），自动同步注册结果

### 红队测试快速开始

```bash
uv run python scripts/redteam/run_redteam.py
```

约 200 条对抗 prompt 检测策略引擎的召回率 / 精确率 / 误拦截率，报告输出至 `scripts/redteam/report.json`。指标定义、目录结构、扩展与限制详见 [docs/redteam.md](docs/redteam.md)。

### HITL 审批与审计闭环

- `execute_code` 等敏感操作与 `review` 级策略命中 → 守卫走 REVIEW，输出挂起（`SUSPENDED` / `pending_review`），待审批请求持久化到 Redis（`moa:hitl:*`，1 小时 TTL）
- 配置飞书后向用户发送审批卡片（通过 / 拒绝按钮），回调 `/webhook/callback` 驱动 FSM 推进：通过则输出送达渠道，拒绝则丢弃
- 每次审批以 `trace_id` 关联：`hitl_decision` + `hitl_duration_ms` 写入审计，可在 Dashboard 安全合规页查看审批耗时分布，形成"拦截 → 审批 → 审计"闭环

## 安全说明

- 所有密钥、Token、Cookie 均通过环境变量注入，`*.env` 和 `logs/` 已加入 `.gitignore`
- 鉴权采用 fail-open 策略：`WEBHOOK_AUTH_TOKEN` / `DASHBOARD_PASSWORD` 未配置时对应鉴权自动关闭，生产环境必须配置
- 提交前请检查是否包含真实 API Key、飞书 App Secret、个人路径或对话日志
- 生产环境建议配合 Vault / K8s Secret 管理密钥，并限制 Dashboard 访问来源

## 技术栈

Python 3.12+ · FastAPI · Redis · httpx · OpenTelemetry · pytest
