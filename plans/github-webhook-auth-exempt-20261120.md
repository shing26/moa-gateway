# Contract — GitHub webhook 鉴权豁免（`/webhook/github/`）

状态：**已冻结**（2026-11-20，A1 部署时实测发现；属于 `GATEWAY_ALLOW_INSECURE=0` 的必要配套）。
裁决方：主会话（审计）；实现方：独立 worktree Implementer；验证方：全新 verifier subagent。

## 修正案 1（2026-11-20，Verifier 因 V3 裸跑失败后）

Verifier 在 HEAD `75f4a8b` 上发现：裸跑 V3 时
`tests/unit/test_delivery_resume.py::test_cmd_delivery_resume_prints_reconcile_view_and_returns_exit_code`
失败。根因不是中间件改动，而是 A1 的
`app/cli.py:_build_push_approval_port` 把
`push approval: feishu (timeout=...)` 打到 **stdout**；当父目录 `.env` 打开
`FEISHU_PUSH_APPROVAL_ENABLED=1` 时，`delivery resume` 的机器可读 JSON 输出被污染，
测试的 `json.loads(captured.out)` 失败。

**裁决**：修根因，不隔离环境、不改测试。

- Allowed paths 增加 `app/cli.py`，范围仅限“把该状态行从 stdout 改为 stderr”；
- 新增 **A6a**：`FEISHU_PUSH_APPROVAL_ENABLED=1` 时，`delivery resume` 的 stdout
  仍必须是可解析 JSON；状态行只能出现在 stderr；
- V3 恢复为**裸跑**（不设 `FEISHU_PUSH_APPROVAL_ENABLED=0`），必须全绿；
- 既有 `test_delivery_resume` 不得修改断言。

## 背景与证据

A1 部署按所有者授权把 `GATEWAY_ALLOW_INSECURE` 设为 `0`、`WEBHOOK_AUTH_TOKEN` 设为非空，
并在 8083 启动服务。GitHub webhook（`/webhook/github/delivery`）返回 **401**。

实测诊断（2026-11-20，本地 8083）：

- 仅带 `X-Hub-Signature-256` 的正确签名 ping → **401**；
- 同一请求额外带 `X-Gateway-Token: <WEBHOOK_AUTH_TOKEN>` → **200 `{"status":"ignored"}`**。

根因：`app/middleware/auth.py` 对所有 `/webhook/*` 要求 `X-Gateway-Token`。GitHub 不会发送
这个自定义头，只发送 `X-Hub-Signature-256`。两个 GitHub 路由本身已经做了 fail-closed 的
HMAC 校验（`app/routes/github_delivery.py:77`、
`apps/code_review_pipeline/routing/github_review_route.py:165` 起），因此中间件这层
token 对 GitHub 通道既不适用、也不应成为第二套无法满足的门。

## 决策

在 `AuthMiddleware.dispatch` 中，对 `/webhook/github/` 前缀**提前放行**，不做
`X-Gateway-Token` 校验；请求随后必须通过路由自身的 `verify_signature`（fail-closed）。

- 只豁免 `/webhook/github/`；`/webhook/{channel}`（聊天渠道）、
  `/webhook/callback`、`/feishu/event` 的行为不变；
- 不删除、不弱化任何签名校验；
- `GITHUB_WEBHOOK_SECRET` 为空且 `GATEWAY_ALLOW_INSECURE=0` 时，路由仍返回 401
  （`auth_not_configured`），豁免不会变成开门；
- 中间件注释说明“provider-verified path”语义，避免未来把豁免扩成 `/webhook/*`。

## Allowed paths

```
app/middleware/auth.py                                  (只加 GitHub 前缀豁免与注释)
app/cli.py                                              (修正案 1：仅状态行 stdout → stderr)
tests/unit/test_auth.py                                 (只追加回归测试)
tests/unit/test_github_delivery_webhook.py              (只追加路由级回归)
tests/unit/test_github_webhook_e2e.py                   (只追加端到端回归)
docs/adr/adr-043-github-webhook-auth-exemption.md       (新 ADR：为什么豁免、边界与 fail-closed 姿态)
plans/github-webhook-auth-exempt-20261120.md            (本文件)
```

## Acceptance criteria

- **A1 有效签名、无 Gateway token**：`WEBHOOK_AUTH_TOKEN` 非空、`GATEWAY_ALLOW_INSECURE=0`、
  `GITHUB_WEBHOOK_SECRET` 非空；带正确 `X-Hub-Signature-256` 的
  `/webhook/github/delivery` ping → 中间件放行，路由返回 200 `ignored`
  （或对 `pull_request` 事件返回 202）。
- **A2 无效/缺失签名、无 Gateway token**：同一配置下 → 401，且不是 500；
  响应体不泄漏是“缺头”还是“算错”。
- **A3 secret 为空**：`GITHUB_WEBHOOK_SECRET` 为空、insecure=0 → 401（路由 fail-closed）。
- **A4 非 GitHub 路径不回归**：`/webhook/test` 在配置了 `WEBHOOK_AUTH_TOKEN` 时
  无 token → 401，带正确 token → 200；`/webhook/callback` 与 `/feishu/event`
  既有行为不变。
- **A5 伪造前缀不绕过**：`/webhook/githubx/...`、`/webhook/github`（无尾斜杠）不得被
  前缀豁免误放行；只匹配 `/webhook/github/`。
- **A6 既有单测全绿**，只允许在既有测试文件末尾追加，不改既有断言。
- **A6a stdout 纯净**：`FEISHU_PUSH_APPROVAL_ENABLED=1` 时，`delivery resume`
  的 stdout 仍是可解析 JSON；`push approval: feishu ...` 只出现在 stderr。
  既有 `test_delivery_resume` 在父 `.env` 打开该开关时也必须通过。
- **A7 证据绑定**：HEAD SHA + `git status --short`。

## Verification commands

```
V1  PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/unit/test_auth.py -q
V2  PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/unit/test_github_delivery_webhook.py tests/unit/test_github_webhook_e2e.py -q
V3  PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/unit -q   (裸跑，不隔离 FEISHU_*)
V4  PYTHONPATH=. .venv/Scripts/python.exe -m ruff check .
```

## Forbidden

- 把豁免扩大到 `/webhook/*` 或全部路径；
- 删除/弱化 `verify_signature`、`approver_gate_error`、`insecure_mode_enabled`；
- 用 `GATEWAY_ALLOW_INSECURE=1` 作为修复手段；
- 改 GitHub 两个路由的签名逻辑、合并状态机、推送审批；
- 顺手改 dashboard Basic auth 或飞书回调路径。

## 合并后的运维动作（主会话执行并留证）

1. 重启 8083 服务使新中间件生效；
2. 触发 GitHub webhook redelivery（或等待下一次 `pull_request`/`check_suite` 事件），
   确认 `last_response.code == 200`；
3. 保留 `GATEWAY_ALLOW_INSECURE=0` 与 `WEBHOOK_AUTH_TOKEN` 非空。
