# ADR-038: dashboard ops 写入口用 DASHBOARD_OPS_WRITE 闸门默认关

日期：2026-11-16
状态：已实施
关联：ADR-037（guard fail-closed）、`app/routes/dashboard.py`（ops config 端点）、
`app/middleware/auth.py`（`insecure_mode_enabled` / `GATEWAY_ALLOW_INSECURE`）

## 背景

`POST /dashboard/api/ops/config` 允许已认证请求热改进程级 `os.environ`（LLM_PROVIDER /
LLM_MODEL / LLM_BASE_URL / LLM_API_KEY）。它排在 Basic auth 之后，**不是匿名洞**；
但"任何已认证用户都能无痕热改全局 LLM 凭证"与治理口径相悖——凭证变更加没有留下
任何需审批的痕迹，外部锐评（2026-11-16，P1）将其列为治理缺口。

## 决策

1. **新增环境开关 `DASHBOARD_OPS_WRITE`，默认关（fail-closed）**：未显式置真值时，
   写入端点返回 403（body 带 `reason` 说明开关未开），不改动任何 `os.environ` 键。
2. **真值语义与 `insecure_mode_enabled` 完全对齐**（1/true/yes/on，大小写不敏感，
   未设置/其他一律关）——实现上直接复用该函数，而不是再写一份接受集合，杜绝两个
   开关的 fail-closed 姿势漂移。
3. **前端最小适配**：写入路径对 403 给出可读提示（指明该动哪个开关），不重构 ops 面板。

**被拒备选：删端点。** 会打断已有 ops 面板 UX（保存/测试表单都指向该端点）。
本仓库既有惯例是"可配置逃生口 + 默认关"（`GATEWAY_ALLOW_INSECURE` 同款）：能力保留，
放行必须显式、可审计（部署环境里看得见这个变量）。

## 后果

- 正面：默认姿态与治理口径一致——凭证热改从"静默可用"变为"显式开闸"；真值语义
  与既有逃生口统一，回归测试（`tests/unit/test_dashboard_ops_gate.py`）双态钉住。
- 负面/运维：需要热改 ops 配置的部署必须显式设 `DASHBOARD_OPS_WRITE=1`，升级后
  原本可用的面板保存按钮会收到 403 与可读提示——这是**预期行为**，不是回归。
- 展示端点（`GET /dashboard/api/ops/config`）不受闸门影响，面板只读能力保持原状。
