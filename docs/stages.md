# MoA Engine — 完整阶段计划

> **当前基线**: `feat/v0.1-mvp`，16 tests passed，Stage 1 scaffolding 已完成  
> **仓库**: `D:\HermesData\moa-gateway`  
> **冻结约束**: 见 `NO_SUBSTITUTES.md`

---

## 总览

```text
Stage 1 [已完成] Scaffolding
  └─ 骨架、FSM、Router、Guard、Engine、基础测试

Stage 2 [进行中] v0.1 MVP
  ├─ 2.1 Redis State Stack + Lua 幂等锁
  ├─ 2.2 Sub-Agent 真实执行 (Coder / General)
  ├─ 2.3 Evaluator AST Guardrail
  └─ 2.4 OTel Tracing + ULID 串联

Stage 3 [待启动] v0.5 Production
  ├─ 3.1 RBAC Guard Sidecar + HITL 卡片
  ├─ 3.2 Provider 级联限流 + Fallback
  ├─ 3.3 ES / VectorDB + Local Async WAL
  └─ 3.4 Redis Sentinel HA + Feature Flag

Stage 4 [远期] v1.0 Full Scale
  ├─ 多渠道 (Discord / Slack)
  ├─ Shadow Migration + Embedding 热切换
  ├─ 合规 SOP (PIPL / 被遗忘权 API)
  └─ K8s HPA + 多 Region 部署
```

---

## Stage 1 [已完成] Scaffolding

**目标**: 建立可运行的最小骨架，冻结后不再重写。

| 模块 | 状态 | 关键文件 |
|------|------|----------|
| FastAPI Gateway | ✅ | `app/main.py` |
| FSM 状态机 | ✅ | `app/fsm/state_machine.py` |
| Router 降级路由 | ✅ | `app/router/intent_router.py` |
| Sub-Agent 契约 | ✅ | `app/agents/contract.py`, `stubs.py` |
| Evaluator 规则 stub | ✅ | `app/evaluator/evaluator.py` |
| Guard Fail-Closed | ✅ | `app/guard/permission_guard.py` |
| Outbound Adapter | ✅ | `app/outbound/adapter.py` |
| Event 模型 | ✅ | `app/models/events.py` |
| Engine 核心 | ✅ | `app/engine.py` |
| Redis State Stack stub | ✅ | `app/redis_state/stack.py`, `store.py` |
| Channel Adapter | ✅ | `app/channels/base.py`, `feishu.py` |
| OTel 模块 | ✅ | `app/observability/tracing.py` |
| Provider Limiter | ✅ | `app/limit_providers/limiter.py` |
| 单元测试 | ✅ | `tests/unit/*.py` (16 passed) |

**冻结要求**:
- `master` 分支禁止直接提交
- Stage 1 文件不再删/重写
- 所有新增走 `feat/*` 分支

---

## Stage 2 [进行中] v0.1 MVP

**目标**: 验证核心链路可端到端运行，所有关键组件有真实实现。

### 2.1 Redis State Stack + Lua 幂等锁

**目标**: 将 `app/redis_state/stack.py` 从 FakeRedis stub 替换为真实 Redis 操作，并加入消息幂等锁。

**交付物**:
- `app/redis_state/lua_lock.py`: Lua 脚本实现原子 `set(nx=True)` + TTL 续期
- `app/redis_state/stack.py`: 移除 FakeRedis，改用 `app/redis_state/store.py` 的 `RedisStateStore`
- `app/redis_state/watcher.py`: Watchdog 续期任务 (防止 Redis 锁 5 秒超时竞态)

**测试**:
- `tests/unit/test_redis_lua_lock.py`: 验证 Lua 锁原子性、TTL、释放
- `tests/unit/test_redis_stack.py`: 验证 push/pop/reset 在真实 Redis (或 FakeRedis 兼容层) 上正确
- `tests/unit/test_redis_watcher.py`: 验证 Watchdog 续期逻辑

**验收标准**:
- `pytest tests/unit -q` 通过，无回归
- `app/redis_state/stack.py` 不再依赖 FakeRedis
- Lua 锁在并发下保证只有一个请求进入 `EXECUTING`

### 2.2 Sub-Agent 真实执行

**目标**: 将 `CoderAgent` / `GeneralAgent` 从 stub 替换为真实 Provider 调用 + 上下文注入。

**交付物**:
- `app/agents/provider.py`: Provider 抽象层 (支持 OpenAI / OpenRouter / 本地模型)
- `app/agents/coder.py`: CoderAgent 真实实现，读取 `AgentEnvelope.global_summary` + `agent_local_slot`
- `app/agents/general.py`: GeneralAgent 真实实现
- `app/agents/executor.py`: `asyncio.Task` Spawn + 超时控制 + 取消传播

**测试**:
- `tests/unit/test_coder_agent.py`: 验证 CoderAgent 调用 Provider + 返回结构化输出
- `tests/unit/test_general_agent.py`: 验证 GeneralAgent 路由到通用对话
- `tests/unit/test_agent_executor.py`: 验证 asyncio.Task Spawn、超时取消、上下文隔离

**验收标准**:
- Sub-Agent 不再 return stub 字符串
- Provider 调用有超时 (2s) + 重试 (retry_count >= 2 时停止)
- 上下文隔离：Agent 只能看到 `global_summary` + `agent_local_slot`

### 2.3 Evaluator AST Guardrail

**目标**: 将 `RuleEvaluator` 升级为 LLM + AST 双重校验。

**交付物**:
- `app/evaluator/llm_eval.py`: LLM 评分器，返回结构化 JSON
- `app/evaluator/ast_guard.py`: Python AST 静态校验 (语法错误、敏感操作、导入白名单)
- `app/evaluator/evaluator.py`: 移除规则级 check，改为 LLM + AST 组合

**测试**:
- `tests/unit/test_llm_eval.py`: 验证 LLM 评分返回合法 JSON
- `tests/unit/test_ast_guard.py`: 验证 AST 拦截危险操作 (eval/exec/import os)
- `tests/unit/test_evaluator.py`: 更新现有测试，验证新 pipeline

**验收标准**:
- Evaluator 输出合法 JSON，失败时 `need_human_review=True`
- AST Guard 拦截 `eval()`, `exec()`, `os.system()` 等危险模式
- retry_count >= 2 时停止重试，生成保守输出 + 审计卡片

### 2.4 OTel Tracing + ULID 串联

**目标**: 将 Webhook → Router → Agent → Evaluator → Outbound 全链路串联。

**交付物**:
- `app/observability/tracing.py`: 增强 `span()` 上下文管理器，自动注入 trace_id
- `app/main.py`: Webhook entrypoint 创建 root span
- `app/engine.py`: handle_event 创建子 span
- `app/router/intent_router.py`: route 创建 span
- `app/agents/executor.py`: Task spawn 创建 span
- `app/evaluator/evaluator.py`: score 创建 span

**测试**:
- `tests/unit/test_tracing_wiring.py`: 验证 trace_id 从 Webhook → Outbound 贯穿

**验收标准**:
- 每个请求有唯一 ULID trace_id
- 可用 Gantt Chart 查看到每个 Span 耗时

---

## Stage 3 [待启动] v0.5 Production

**目标**: 安全、审计、高可用。

### 3.1 RBAC Guard Sidecar + HITL 卡片

**目标**: 将 `FailClosedPermissionGuard` 升级为独立 Sidecar 服务。

**交付物**:
- `app/guard/sidecar_client.py`: gRPC 客户端，调用外部 Guard Sidecar
- `app/guard/rbac.py`: RBAC 策略定义 (角色、权限、资源)
- `app/hitl/card.py`: 飞书 HITL 卡片构建器
- `app/hitl/dispatcher.py`: HITL 审批流转

**测试**:
- `tests/unit/test_guard_sidecar.py`: 验证 Sidecar 调用、超时、Fail-Closed
- `tests/unit/test_rbac.py`: 验证 RBAC 策略匹配
- `tests/unit/test_hitl_card.py`: 验证卡片 JSON 结构

### 3.2 Provider 级联限流 + Fallback

**目标**: 多 Provider 级联，A 429 时自动切 B。

**交付物**:
- `app/limit_providers/circuit_breaker.py`: 熔断器实现
- `app/limit_providers/fallback_chain.py`: Provider 级联配置
- `app/router/intent_router.py`: 接入熔断器 + Fallback

**测试**:
- `tests/unit/test_circuit_breaker.py`: 验证熔断状态机 (CLOSED/OPEN/HALF-OPEN)
- `tests/unit/test_fallback_chain.py`: 验证 A 失败后自动切 B

### 3.3 ES / VectorDB + Local Async WAL

**目标**: Audit Log + 上下文持久化。

**交付物**:
- `app/observability/audit.py`: ES 写入客户端
- `app/observability/wal.py`: Local Async WAL (1GB 磁盘队列)
- `app/rag/context.py`: Session 级 RAG 上下文

### 3.4 Redis Sentinel HA + Feature Flag

**目标**: 生产级高可用 + 热切换。

**交付物**:
- `app/redis_state/sentinel.py`: Sentinel 连接池
- `app/config/flags.py`: Feature Flag 客户端 (Redis Dynamic Config)

---

## Stage 4 [远期] v1.0 Full Scale

**目标**: 规模化运营。

| 模块 | 描述 |
|------|------|
| 多渠道 Adapter | Discord / Slack 适配器 |
| Shadow Migration | V2 双写 + Resumable Worker |
| Embedding 热切换 | 模型版本切换 + Canary |
| 合规 SOP | PIPL / 被遗忘权 API |
| K8s HPA + 多 Region | 自动扩缩容 + 跨境隔离 |

---

## 执行节奏

每个 Stage 遵循：

1. **Create branch**: `git checkout -b feat/<stage>-<task>`
2. **Implement**: 实装模块 + 测试
3. **Verify**: `PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/unit -q`
4. **Commit**: 单任务单 commit，信息清晰
5. **Review**: 提交后等待用户验收
6. **Merge**: 用户确认后 merge 到 `feat/v0.1-mvp`

**禁止**:
- 未跑测试就 commit
- 修改 Stage 1 已提交文件
- 在 `master` 分支直接提交
- 删除 `pyproject.toml` 既有依赖

---

## 当前阻塞项

Stage 2 唯一阻塞项：**真实 Redis 实例可用** (Docker / 本地 / 远程)。

启动命令：
```bash
cd D:\HermesData\moa-gateway
docker compose -f docker-compose.dev.yml up -d redis
```

若无 Docker，可修改 `app/redis_state/store.py` 的 `RedisConfig.url` 指向远程 Redis。
