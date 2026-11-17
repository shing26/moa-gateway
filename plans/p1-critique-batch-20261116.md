# 契约：锐评 P1 批次（p1-critique-batch-20261116）

状态：**已冻结**（2026-11-16，冻结后不得修改；实现偏离须回到主会话重新冻结）
裁决方：主会话（审计）；实现方：独立 worktree Implementer；验证方：全新 verifier subagent

## 背景与决策

2026-11-16 外部锐评查证后的 P1 修复批次，三项：

### 1. dashboard ops 写入口加显式开关（默认关闭，fail-closed）

现状：`POST /dashboard/api/ops/config`（app/routes/dashboard.py ~198 行）允许**已通过
Basic auth 的请求**热改进程级 `os.environ`（含 `LLM_API_KEY`）。它在 Basic auth 之后、
未配密码时 fail-closed（401），不是匿名洞——但"已认证用户可无痕热改全局凭证"与
本项目的治理口径相悖，且写 os.environ 的配置重载语义不明。

决策：新增环境开关 `DASHBOARD_OPS_WRITE`，语义与 `insecure_mode_enabled`
（app/middleware/auth.py:31）一致——仅显式真值（1/true/yes/on）放行：

- 开关未显式置真值：端点返回 **403**（body 含 `reason`，说明开关未开），**不得**
  修改任何 os.environ 键；
- 开关显式置真值：行为与现状完全一致；
- 前端 app/static/dashboard.js 的写入路径（~821 行）对 403 给出可读提示（最小改动，
  不重构面板）。

被拒备选：直接删除端点——会打断已有 ops 面板 UX，且"可配置的逃生口 + 默认关"
是本仓库既有惯例（GATEWAY_ALLOW_INSECURE 同款）。

### 2. tests/integration 进 CI（独立 job，非 required）

现状：CI 只跑 tests/unit；tests/integration 9 个文件有完整的自跳过设计
（服务缺席即 skip，不伪造通过）与本地恢复路径（docker compose）。

决策：新增独立 job `integration`：

- services：postgres（pgvector/pgvector:pg16，宿主 5433→容器 5432）+ redis
  （redis:7，宿主 6380→容器 6379），与 docker-compose.dev.yml 端口约定一致；
- env：`CODE_REVIEW_DATABASE_URL`（指向 job 内 postgres，postgres://gateway:gateway@localhost:5433/gateway）
  与 `REDIS_URL=redis://localhost:6380/0`；
- 步骤：checkout → uv sync --all-extras --dev → `uv run pytest tests/integration -q -ra`
  （`-ra` 让 skip 原因可见，不掩盖）；
- **不**加入分支保护的 required checks（先观察若干次运行再议）——strict 保护配置
  本身不在本契约修改范围内。

### 3. pyproject.toml dev 依赖声明去重

现状：`[project.optional-dependencies].dev` 同一组依赖声明了两次（约 24/27 行）。
删除重复的一组。判定：纯配置去重、无行为变更，并入本契约但验收轻量
（`uv lock --check` + `uv sync --all-extras --dev` 成功即可）。

## Allowed paths（仅限以下文件）

- `app/routes/dashboard.py`（仅 ops config 端点的开关逻辑）
- `app/static/dashboard.js`（仅写入路径的 403 提示）
- `.github/workflows/ci.yml`（仅新增 integration job）
- `pyproject.toml`（仅删除重复的 dev 依赖声明）
- `tests/unit/` 下新增/修改的回归测试文件
- 新增 `tests/unit/test_dashboard_ops_gate.py`（或并入现有 dashboard 测试文件）
- 新增 `docs/adr/adr-038-dashboard-ops-write-gate.md`

## Acceptance criteria（Verifier 逐条判定）

- **A1** 开关关：POST ops/config 返回 403 且 `os.environ` 的五个键
  （LLM_PROVIDER/LLM_MODEL/LLM_BASE_URL/LLM_API_KEY）无一被改；开关开：
  行为与修复前一致（回归测试双态覆盖）。
- **A2** 开关判定复用/对齐 `insecure_mode_enabled` 的真值语义（1/true/yes/on，
  其余含未设置一律关）。
- **A3** 前端 403 有可读提示；不引入新依赖。
- **A4** ci.yml 新增 integration job：services 端口与 compose 约定一致、
  env 完整、步骤含 `-ra`；`test` job 与分支保护配置零改动。
- **A5** pyproject 仅删除重复 dev 声明；`uv lock --check` 通过。
- **A6** 本地 `docker compose -f docker-compose.dev.yml up -d` 后
  `uv run pytest tests/integration -q` 全绿或全部 skip 且无 fail
  （本地环境允许 skip；CI 侧由远端 CI 证明）。
- **A7** 现有单测全绿（≥ 2016 基线只增不减）。
- **A8** 证据绑定 HEAD SHA + `git status --short`。

## Verification commands（Verifier 亲自重跑）

```bash
uv run pytest tests/unit -q
uv run pytest tests/integration -q -ra   # 允许 skip，不允许 fail
uv run ruff check .
uv run bandit -r app -q -s B105,B107,B110,B112,B324
uv run python -c "import app.main"
uv run python scripts/check_no_source_patch.py
uv lock --check
```

## Forbidden

- 不得修改分支保护配置、required checks、`test` job 本身
- 不得删除 ops config 端点或 GET 端点
- 不得修改 app/guard/、台账、其他 ADR、README
- 不得弱化任何现有测试断言；不碰 tests/integration/ 的测试逻辑本身
- pyproject 除 dev 依赖去重外不得改动其他键
