# 交接文档：moa-gateway → agent-gateway 改造（给编码 AI harness）

> 读者：接手的 AI 编码 harness（Codex / Claude Code / Cursor / 其他 Agent）。
> 任务来源：用户简历项目需求（Agent 工程岗 + 后端/全栈岗双轴叙事）。
> 完整计划见 `docs/RESUME_V2_PLAN.md`（本文件是它的执行版，含命令与坑）。
> **先读本文件 + `docs/RESUME_V2_PLAN.md` + `README.md`，再动手。**

---

## 0. 交接摘要（30 秒版）

- 仓库：`D:\HermesData\moa-gateway`（Windows，bash shell）
- 现状：FastAPI 多 Agent 网关 + GitHub PR 审查子应用，362 测试通过 / 1 失败 / 9 跳过
- 任务：5 个 Phase —— 换 LiteLLM 内核、加 Eval 评估体系、工程化 CI、PR 审查演示接入、README 包装
- 硬约束：不推倒重来、不删依赖、先处理未提交改动、不提交密钥
- 失败测试根因已定位：`app/agents/provider.py` 空 env 值崩溃（Phase 0 修）

---

## 1. 环境与命令（Windows）

```bash
cd /d/HermesData/moa-gateway

# 测试（完整基线约 46s）
.venv/Scripts/python.exe -m pytest tests/unit -q
# 或
uv run pytest tests/unit -q          # uv 已安装时

# 跑单个测试
.venv/Scripts/python.exe -m pytest tests/unit/test_xxx.py -q

# lint / 安全
uv run ruff check .
uv run bandit -r app -q

# 启动服务（本地）
.venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080
# 健康检查：http://localhost:8080/health   Dashboard：http://localhost:8080/dashboard
```

- 依赖管理：`pyproject.toml` + `uv.lock`。**改依赖用 `uv add <pkg>`，不要手改 lock。**
- Python 版本：pyproject 声明 >=3.12；本地 venv 是 3.11（能用）；CI 是 3.12。语法按 3.12 写，但别依赖 3.12-only 运行时特性在本地验证不到的行为（本地会先跑）。

## 2. 代码地图

### 2.1 请求链路（现状）

```
飞书/Webhook → AuthMiddleware → rate_limiter → Engine(FSM) → IntentRouter(正则→微模型→LLM)
  → get_agent(intent) → Coder/General(LLM + 工具循环≤3轮)
  → RuleEvaluator(AST) → GuardService(行为+输出策略) → HITL(Redis+飞书卡片) / DENY
  → ResponseAdapter → 返回；log_request 写审计 WAL(JSONL)+可选 ES
```

### 2.2 关键文件职责表

| 文件 | 职责 | 改造 |
|---|---|---|
| `app/main.py` | FastAPI 装配 + 中间件 | 仅 title（Phase 5） |
| `app/deps.py` | 全部单例（engine/pipeline/memory/guard/flag/retriever） | **不动** |
| `app/engine.py` | FSM 推进 + HITL 存储 | **不动** |
| `app/pipeline.py` | `MoAPipeline.run()` 主管线 | 仅 cost 透传（Phase 1） |
| `app/router/intent_router.py` | 三级降级路由 | 不动 |
| `app/agents/provider.py` | 手写 httpx LLM 客户端（`LLMClient`） | **Phase 1 换 LiteLLM** |
| `app/agents/stubs.py` | Coder/General 执行 + 工具循环（`_execute_with_tools`） | **仅加指标透传**（`_execute_with_runtime_or_injected` 写 `llm_metrics`） |
| `app/agents/tools.py` | 工具注册表（knowledge_search/current_time/execute_code） | 不动 |
| `app/guard/guard_service.py` | `GuardService.evaluate/evaluate_output`（ALLOW/REVIEW/DENY） | 不动（Eval 用） |
| `app/guard/policies.py` | 策略引擎（内网IP/密钥/价格承诺） | 不动（Eval 用） |
| `app/evaluator/evaluator.py` | `RuleEvaluator.score`（AST 静态） | 不动（Eval 用） |
| `app/middleware/request_logger.py` | `log_request()` 写 WAL | Phase 1 加 cost 参数 |
| `app/audit/models.py` | `AuditEntry` | Phase 1 加字段（走 extra） |
| `app/redis_state/memory_fallback.py` | 内存回退 store | **不动**（已知 eval 缺陷，不修） |
| `app/vectordb/__init__.py` | 关键词检索（中文 bigram） | 不动（README 如实描述） |
| `app/command_mode.py` | `/coding` 等指令 | Phase 4 加 `/review` |
| `app/routes/webhook.py` | `/webhook/{channel}` + `/webhook/callback` | 不动 |
| `apps/code_review_pipeline/*` | GitHub PR 审查（Triage→静态→语义RAG→测试→报告） | Phase 4 演示接入 |
| `tests/unit/*` | 39 个文件，362 通过 | 补新测试 |

## 3. 当前基线（必须知道）

- `pytest tests/unit -q`：**362 passed / 1 failed / 9 skipped**
- 失败：`tests/unit/test_github_webhook_e2e.py::test_github_review_webhook_returns_accepted`
  - 根因：`app/agents/provider.py` `from_env()` 里 `int(os.getenv("LLM_MAX_TOKENS", "4096"))`，测试注入 `LLM_MAX_TOKENS=''` → `ValueError`
  - 修复：所有 `int(os.getenv(k, d))` / `float(...)` 改为 `int(os.getenv(k) or d)`（Phase 0）
- 工作区脏：有一批已修改文件 + 6 个未跟踪测试 + `data/` 未跟踪目录
  - **开工第一步**：与用户确认后 `git add -A && git commit -m "chore: checkpoint before resume-v2"` 或 stash；不要带着脏状态改
- 分支：本地 `main`；**新分支 `feat/resume-v2`**，所有提交走它，不直接提交 main

## 4. 改造任务清单（按顺序执行）

### Phase 0：基线修复 + 分支（0.5 天）
- [ ] 处理未提交改动（commit/stash，问用户）
- [ ] `git checkout -b feat/resume-v2`
- [ ] 修 `app/agents/provider.py` 空 env 崩溃（见 §3）
- [ ] `pytest tests/unit -q` 全绿
- [ ] commit：`fix: tolerate empty env vars in LLMConfig.from_env`

### Phase 1：换内核 LiteLLM + fallback + 成本（1-2 天）
- [ ] `uv add litellm`
- [ ] `app/agents/provider.py`：保留 `LLMClient` 接口（`chat`/`chat_with_tools`/`aclose`/`__aenter__`/`__aexit__`），内部换 `litellm.acompletion`；加 `fallback_models` 列表与失败重试；加 `cost_usd`/`latency_ms`/`model_used` 记录（`litellm.cost_per_token` 或 response 自带 cost）
- [ ] `app/audit/models.py` + `app/middleware/request_logger.py`：`extra` 增加 `llm_model`/`cost_usd`/`llm_latency_ms`/`fallback_used`（默认值，不破坏现有调用）
- [ ] 新增 `tests/unit/test_provider_litellm.py`：monkeypatch `litellm.acompletion` 验证参数透传、fallback 切换、cost 计算、空 env 不崩
- [ ] 全量测试 + commit：`feat(llm): swap to litellm with fallback and cost tracking`
- 注意：现有测试大多注入 fake LLM，不受影响；**禁止让测试打真实网络**

### Phase 2：Eval 评估体系（3-4 天）★核心卖点
- [ ] 建 `evals/datasets/intent.jsonl`（50 条）、`guard_redteam.jsonl`（50 条）、`e2e.jsonl`（30 条）
  - schema 见 `docs/RESUME_V2_PLAN.md` §6.2
  - **至少 5 条手写边界用例**（如"今天天气怎么样"→assistant、多意图、含内网 IP 的对抗文本）
- [ ] 建 `evals/run_evals.py`：
  - intent：`IntentRouter().route(text)`（不调 LLM）
  - guard：`guard_service.evaluate_output(text, hitl_enabled=False)` → expected_action 对比
  - e2e：`MoAPipeline.run()` + `evals/judge.py`（LLM-as-judge 打分）；`--offline` 时用 fake 并标 skipped
  - 输出 `evals/reports/latest.json`（指标格式见 PLAN §6.3）
- [ ] 新增 `tests/unit/test_evals_runner.py`：小 fixture 数据验证指标计算
- [ ] `uv run python evals/run_evals.py --offline` 出报告；intent 准确率 ≥0.9、guard deny 召回 ≥0.95（否则调数据集，**不许调阈值凑数**）
- [ ] commit：`feat(evals): add intent/guard/e2e evaluation harness`

### Phase 3：工程化（1-2 天）
- [ ] `.github/workflows/ci.yml`：加 `ruff check .`、`bandit -r app -q`、`python evals/run_evals.py --offline`
- [ ] 检查并修 `Dockerfile`（python:3.12 + uv 安装可用）
- [ ] lint 存量告警：能修的顺手修（小 diff），不能修的加 `# noqa` 或 ruff ignore 配置，**别大改码风**
- [ ] commit：`chore(ci): extend CI with lint, bandit, eval smoke`

### Phase 4：垂直场景接入（1-2 天）
- [ ] `app/command_mode.py` 加 `/review` 命令（intent: `review`）
- [ ] 新增 `app/agents/review_agent.py`：包装 `CodeReviewPipeline`，输入 `owner/repo#PR号`，输出审查摘要文本；无 GITHUB_TOKEN 等配置时返回优雅降级文案
- [ ] 注册进 `app/agents/loader.py`（`get_agent('review')`）
- [ ] 新增 `tests/unit/test_review_agent.py`：注入 fake github client（参考 `tests/unit/test_github_webhook_e2e.py` 的 fixture）
- [ ] commit：`feat(review): expose PR review as a gateway agent command`
- 风险提醒：`CodeReviewPipeline` 用 `GitHubClient.from_env()`，测试必须注入 fake，禁止真实网络

### Phase 5：包装（1-2 天）
- [ ] `README.md` 重写：标题 `agent-gateway`、mermaid 架构图、三卖点章节（路由省钱数据 / 守卫+HITL redteam 100% / Eval 报告截图）、垂直应用章节、快速开始（含 eval 命令）、"为什么不用 LangChain/LangGraph"选型说明
- [ ] `app/main.py` title 改 `Agent Gateway`；Dashboard 标题文案同步
- [ ] `pyproject.toml` name 改 `agent-gateway`（可选，改后 `uv lock` 需重生成；若影响大可只改展示层）
- [ ] demo：录屏 + 部署链接（可选：Render/Railway/Fly 或 Cloudflare Tunnel）
- [ ] commit：`docs: rewrite README for resume-v2 positioning`

## 5. 编码约束（硬性）

1. **不推倒重来**：现有模块是资产；改动最小化，不重构无关代码
2. **依赖只增不减**：`pyproject.toml` 不许删既有依赖；加依赖用 `uv add`
3. **每个功能配测试**：新增/修改逻辑必须有 `tests/unit/` 对应测试
4. **分支纪律**：所有提交在 `feat/resume-v2`；不直接提交 `main`；单任务单 commit
5. **密钥纪律**：不读不提交 `.env`（真实密钥）；`logs/`、`data/` 已在 .gitignore 或应加入；README 不许出现真实 key/路径
6. **测试纪律**：不许跳过既有测试；不许为了绿而改断言含义（`--offline` 的 e2e skip 除外，这是设计行为）
7. **提交前**：`pytest tests/unit -q` 必须全绿（Phase 各自验收通过才 commit）

## 6. 已知的坑（避免踩）

1. **Windows 路径**：仓库在 `D:\`，bash 里用 `/d/...`；Python 路径分隔符注意
2. **CRLF**：仓库文件多为 CRLF（Windows），编辑时尽量保留原行尾，避免整文件 diff
3. **`MemoryStateStore.eval()` 返回 False**（`app/redis_state/memory_fallback.py`）：内存回退时 Lua 锁不生效——**这是已知降级语义，不要"修复"它**，除非任务明确要求
4. **同步桥 `_SyncBridge`**（`app/memory.py`）：同步线程跑 asyncio loop，别在测试里依赖它做时序断言
5. **e2e eval 需要 LLM**：CI 里只跑 `--offline`；本地跑全量需要 `.env` 里的 API key，别把 key 写进测试
6. **测试注入 fake LLM 的惯例**：`app/agents/stubs.py` 的 `_execute_with_runtime_or_injected(llm, ...)` 支持注入；`LLMClient` 换实现后保持该惯例
7. **`test_github_webhook_e2e.py` 是未跟踪文件**：它属于本地新加测试，commit 时一起带上；它现在失败是 env 注入问题（Phase 0 修）
8. **lint 存量**：`ruff check .` 当前可能有存量告警（未实测）；CI 加 ruff 前先本地跑，别让 CI 第一天就红
9. **`uv.lock`**：改动依赖后 `uv sync` 保证 lock 与 pyproject 一致
10. **`data/` 目录**：未跟踪，是运行产物，确认 .gitignore 覆盖或手动忽略，不要 commit 进去

## 7. 环境变量参考（`.env.template` 为准，这里只列改造相关）

| 变量 | 用途 | 备注 |
|---|---|---|
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `LLM_MODEL` | 主 LLM | LiteLLM 替换后仍走这些 |
| `LLM_MAX_TOKENS` / `LLM_TIMEOUT` / `LLM_TEMPERATURE` | LLM 参数 | **空值会崩，Phase 0 修** |
| `REDIS_URL` / `REDIS_ENABLE_FALLBACK` | 会话/HITL 存储 | 无 Redis 自动内存回退 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书 + HITL 卡片 | 可选 |
| `WEBHOOK_AUTH_TOKEN` / `DASHBOARD_PASSWORD` | 鉴权（fail-open） | 留空=不鉴权 |
| `HITL_ENABLED` | 是否启用审批 | 默认 false |
| `GITHUB_TOKEN` | PR 审查（Phase 4） | 无则降级 |
| `CODE_REVIEW_DATABASE_URL` | PR 审查存储（Postgres） | 无则内存存储 |
| `ES_HOSTS` / `ES_INDEX_PREFIX` | 审计 ES | 可选 |

## 8. 完成定义（DoD，全部满足才算交付）

- [ ] `pytest tests/unit -q` 全绿（≥363，含新增测试）
- [ ] CI 全绿（pytest + ruff + bandit + eval offline）
- [ ] `evals/run_evals.py --offline` 出报告，指标真实
- [ ] LiteLLM 替换后接口兼容，fallback/cost 有测试
- [ ] README 重写完成（架构图 + 三卖点 + 数据表 + 选型说明 + demo）
- [ ] PR 审查演示路径可用（或 fixture 测试通过 + README 截图）
- [ ] 工作区干净：无 `.env`/`logs/`/`data/` 提交
- [ ] 所有 commit 在 `feat/resume-v2`，提交信息清晰
- [ ] 向用户交付：改动摘要 + 测试结果 + README + demo 链接 + 简历条目草稿

## 9. 交接检查清单（给接手的 harness）

- [ ] 已读 `docs/HANDOFF.md`、`docs/RESUME_V2_PLAN.md`、`README.md`
- [ ] 已确认测试基线（362/1/9）
- [ ] 已与用户确认未提交改动的处理方式
- [ ] 已建 `feat/resume-v2` 分支
- [ ] 按 Phase 0→5 顺序执行，每 Phase 验收后 commit
