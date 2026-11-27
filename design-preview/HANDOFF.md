# Dashboard 重设计 · 设计交接说明（供 codex / claude 接手开发）

> 状态：**设计已定稿，开发未开始**。本目录只含设计资产，产品代码零改动。
> 设计稿：`dashboard-design-preview.html`（自包含，3 视图切换，可直接打印 PDF）。
> 视觉沿用 `app/static/tokens.css` 的 oklch bright SaaS 体系（anchor hue 190），与现有页面同一套 design token。

## 一、为什么重设计（demo 感三主因，实测结论）

数据是真实的，壳是 demo 的。三版方案都不编造数据，各补一条"信息 → 动作"闭环。

1. **页面自曝本机 demo**：`dashboard_html.py:112,257-259` 写死 `v0.1.0·管理后台`、`本地模式·数据仅本机可见`；测试台默认 `hello/dash-test/admin`；对话示例"帮我算 3*7"。
2. **最真实闭环在页面外**：HITL 审批（飞书卡片/collab API/CLI）、PR review（GitHub 评论）、审计链全量校验（CLI）都不在 dashboard；安全页近 7 天 `guard_action`/`hitl_decision` 实测全空。
3. **无身份/无时间/无下一步**：chat 的 `user_id` 前端自由填；数据无时间戳/样本量；错误态把 `请求失败:500` 塞进空状态（`dashboard.js:158-161`）。

## 二、选定路线：C 打底 + A 择优叠加

- **C 可信度中心（打底）**：核心对象=证据，每个数字标注来源/窗口/样本量，把 README:478-481 的局限搬进 UI。后端改动最小。
- **A 值班台（择优叠加）**：核心对象=事件。只叠加有真实后端支撑的四项，其余排除。

**A 的取舍边界（重要，防止方案膨胀）**
- ✅ 做：数据新鲜度时间戳（healthz 已有 5s 缓存）、审计派生事件时间线（从 `guard_action`/`hitl_decision` 聚合）、审计链全量校验入口（复用 `scripts/verify_audit_chain.py`）、已有 flag/ops 处置动作。
- ❌ 排除：SLO 面板、依赖拓扑图、事件认领/工单状态机（无持久化，会重蹈 HITL 重启必败的覆辙）。

## 三、设计稿三视图（对应交付物）

| 视图 | 内容 | 治 demo 感的关键 |
|---|---|---|
| 一 · 可信度总览（C） | 带来源标注的指标卡（Hit@1 0.826/n=23、e2e n=30、成本"不展示"）、指标下钻面板、5 条已知局限清单 | 数字有 provenance；诚实局限进 UI |
| 二 · 事件台（A） | 数据截止秒数、审计派生事件时间线、详情抽屉、切 Flag/改配置处置、ADR-038 闸门说明 | 发现→定位→处置→留痕闭环 |
| 三 · 状态库 | 空/加载/错误/权限四态，C 与 A 对照 | 错误≠空，不再拿 500 冒充空状态 |

稿内所有数值为**布局占位**，实体名/口径取自真实代码（见下）。交接开发时数值必须接真实端点，不得照抄。

## 四、真实数据源与字段契约（已实测，勿凭记忆改）

- `evals/reports/latest.json` 顶层键：`generated_at, git_sha, engine, intent, intent_consistency, guard, tool_selection, hitl_feedback, agent_metrics, e2e, retrieval, summary`。⚠️ 该文件会被 CI `--offline` 覆盖导致头条指标 0.0，**不可当活体结论**；端点必须同时返回 `generated_at` + `git_sha` 明示快照。
- `evals/reports/eval-baseline-v1.json` 顶层键：`rows, protocol, stamp, model, env_overrides, gate_state_arm`。
- `evals/datasets/retrieval_gold.jsonl`：实测 **23 行**（人工抽检）。
- 审计日志：`logs/audit-*.jsonl`，读侧 `app/services/audit_stats.py`；写侧 `app/audit/recorder.py`。近 7 天窗口实测无 `guard_action`/`hitl_decision`。
- 现有实验台只读端点：`app/routes/experiments.py:23-47`（`/dashboard/api/experiments`、`/experiments/{batch_id}`、`.../runs/...`），数据来自 `.bench-experiments`。
- 鉴权：`app/middleware/auth.py`，Basic 单账号 `admin`；`approver_gate_error()` 审批人白名单（:36-58）；写闸门 `DASHBOARD_OPS_WRITE`（ADR-038，默认关）仅覆盖 `POST /dashboard/api/ops/config`。

## 五、分期改造清单（精确到落点，供接手开发）

### Phase 0 · 去 demo 感止血（纯前端 + 2 处微改）
| 改动 | 文件:行号 | 动作 | 验证 |
|---|---|---|---|
| 去自曝文案 | `dashboard_html.py:112,257-259` | `v0.1.0·管理后台`→`Gateway Console`；删"本地模式·数据仅本机可见" | `test_dashboard_routes.py`（断言侧栏文案处同步改） |
| 去玩具默认值 | `dashboard_html.py:387-391,580`、`dashboard.js:519-522` | `hello/dash-test/admin`→空值+真实 placeholder；对话示例改真实业务例句 | `test_dashboard_static_js.py` |
| 硬编码 limiter | `dashboard.py:192` | 改 `{"wired": False, "source": "static"}` 并在 UI 标"未实现" | ops/config 形状契约测试 |
| 错误态修正 | `dashboard.js:158-161` | 失败不再写 `emptyState(err.message)`，独立 error 区块+重试按钮 | 前端单测 |

### Phase 1 · C 可信度中心打底（1 新端点 + 1 新页面）
| 改动 | 文件 | 动作 |
|---|---|---|
| 聚合端点 | `app/routes/dashboard.py`（仿 177-195 只读风格） | `GET /dashboard/api/evidence`，从 latest.json + retrieval_gold.jsonl 拼装 |
| 字段契约 | `app/schemas/dashboard.py` 新增 `EvidenceMetric` | 每指标带 `value, source_file, generated_at, sample_n, synthetic, caveat` 六字段（C 版灵魂，缺一不可） |
| 新页面 | `dashboard_html.py` 加 `evidence_page()` + `PAGES` 条目；`dashboard.py` 加 `@router.get("/dashboard/evidence")` | 三张指标卡 + 下钻面板 + 局限清单 |
| 前端 | `dashboard.js` 加 `initEvidence()` + 注册到 DOMContentLoaded 分发器（:882-899） | 取数渲染；synthetic=true 显黄标；失败显 error 态不猜值 |

验证：`pytest tests/unit/test_dashboard_routes.py tests/unit/test_dashboard_static_js.py -q`；新增 `test_evidence_api.py` 断言六字段齐全。

### Phase 2 · A 择优叠加（3 新端点）
| 改动 | 文件 | 动作 |
|---|---|---|
| 新鲜度字段 | `health.py` healthz + 各 `/dashboard/api/*` | 统一加 `as_of`（ISO 时间），前端顶部显示"数据截止 X 秒前" |
| 事件派生 | `app/services/audit_stats.py` 加 `derive_events()`；`dashboard.py` 加 `GET /dashboard/api/events` | 筛 `guard_action∈{deny,review}`、`hitl_decision` 非空、超时事件 → 事件对象 |
| 审计链校验 | `dashboard.py` 加 `GET /dashboard/api/audit/verify` | 把 `scripts/verify_audit_chain.py` 核心提成可 import 模块，包只读 API |
| 处置接入 | `dashboard.js` 事件详情抽屉 | 接已有 flag set/delete；危险操作二次确认 + ADR-038 的 403 可读提示；**处置写审计** |
| 事件页 | `dashboard_html.py` 加 `events_page()` + 路由 | 时间线 + 详情抽屉，复用 Phase 1 组件 |

验证：新增 `test_events_derive.py`（真实 audit JSONL 片段）；审计链端点复用现有 verify 测试；处置复用 `test_dashboard_ops_gate.py`。

## 六、工程纪律（来自项目踩坑记录，务必遵守）

1. **台账会腐烂**：引用任何数字前先 `git fetch` + `git rev-list --left-right --count main...origin/main`，仓库以实测为准。
2. **测试数只能实跑**：`pytest --collect-only -q`，grep 一定低估（漏 parametrize）。
3. **精确 grep**：用 `grep -rn "实体("`，不要 `-ril`（子串假阳性）。
4. **C: 盘易满**：跑大测试前清 `%TEMP%` 的 `pytest-of-*`。
5. **不得虚构**：设计稿数值是占位；开发时接真数据，禁止为"好看"造假指标。
6. 当前分支：`fix/profile-verify-interpreter` @ `9c48c3c`；`main` 与 `origin/main` 同步（0 0）。

## 七、交接状态

- ✅ 设计定稿（三视图高保真稿 + 本说明）
- ⬜ 开发未开始 —— 由 codex / claude 接手
- 如需变更设计路线，先回到设计阶段，不要在开发期边写边改设计。
