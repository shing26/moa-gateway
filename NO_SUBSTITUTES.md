# MoA Engine — Stage 1 Handoff Checklist

## 完成交付物 (Stage 1 已完成)
- [x] Git 仓库初始化，首个 commit 可回滚
- [x] `pyproject.toml` + `Dockerfile` + `docker-compose.dev.yml`
- [x] FastAPI health endpoint (`app/main.py`)
- [x] 配置骨架 (`app/config.py`) — 无外部依赖，本地可导入
- [x] FSM 状态机 + 状态转移矩阵 (`app/fsm/state_machine.py`)
- [x] 事件模型 + ULID trace (`app/models/events.py`)
- [x] Router 协议 + 路由降级骨架 (`app/router/intent_router.py`)
- [x] Sub-Agent 注册契约 + stub (`app/agents/contract.py`, `app/agents/stubs.py`)
- [x] Evaluator 规则引擎 stub (`app/evaluator/evaluator.py`)
- [x] Guard Fail-Closed stub (`app/guard/permission_guard.py`)
- [x] Outbound response adapter (`app/outbound/adapter.py`)
- [x] Engine 核心骨架 (`app/engine.py`)
- [x] 9 个单元测试，全部通过 (`pytest tests/unit -q`)

## 阶段 2 要求 (Stage 2 必须遵守)
- [ ] 不重写/删除 Stage 1 任何已提交文件
- [ ] 保持 `app/` 目录结构，新增模块放对应包内
- [ ] 所有新增代码补对应 `tests/unit/` 测试
- [ ] 不能合并 dev 分支的未测试代码
- [ ] 修改 `pyproject.toml` 只增不减，不删既有依赖

## 阶段 2 目标 (v0.1 MVP)
1. Redis State Stack 实现 + Lua 幂等锁
2. Feishu HTTP Adapter + Webhook Endpoint
3. 2 个 Sub-Agent (Coder / General) 真实执行
4. Evaluator + AST Guardrail 完整实现
5. Provider 级联限流
6. OTel Tracing + ULID 串联

## 阶段 2 启动命令 (确认后执行)
```bash
cd <repo-root>
git checkout -b feat/v0.1-mvp
codex exec --full-auto "根据 NO_SUBSTITUTES.md 的 Stage 2 要求，实现 v0.1 MVP 并让测试通过"
```

## 分支维护
- `master`：Stage 1 冻结版本，禁止直接提交
- `feat/*`：所有功能开发分支
- `main`（未来）：仅限合并 PR 后创建
