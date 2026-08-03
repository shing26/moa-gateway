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

## 目录结构

```text
app/
  agents/            Sub-Agent 注册与 LLM Provider
  audit/             审计模型与 JSONL WAL
  channels/          飞书渠道适配（消息 / 事件 / 卡片 / Token）
  evaluator/         输出评估器
  fsm/               状态机
  guard/             权限与守卫服务
  knowledge.py       知识库分块与管理
  memory.py          会话记忆（内存实现）
  middleware/        请求日志、Feature Flag
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

# Redis（默认 localhost:6379，生产可配 Sentinel）
REDIS_URL=redis://localhost:6379/0
# REDIS_SENTINEL_HOSTS=host1:26379,host2:26379,host3:26379
# REDIS_SENTINEL_MASTER=mymaster

# 飞书（可选，HITL 审批卡片需要）
# FEISHU_APP_ID=cli_xxx
# FEISHU_APP_SECRET=xxx

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

## 安全说明

- 所有密钥、Token、Cookie 均通过环境变量注入，`*.env` 和 `logs/` 已加入 `.gitignore`
- 提交前请检查是否包含真实 API Key、飞书 App Secret、个人路径或对话日志
- 生产环境建议配合 Vault / K8s Secret 管理密钥，并限制 Dashboard 访问来源

## 技术栈

Python 3.12+ · FastAPI · Redis · httpx · OpenTelemetry · pytest
