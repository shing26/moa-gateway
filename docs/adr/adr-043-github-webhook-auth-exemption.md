# ADR-043: GitHub webhook 的中间件鉴权豁免——只豁免 provider-verified 路径

日期：2026-11-20
状态：已实施
关联：`plans/github-webhook-auth-exempt-20261120.md`、ADR-021（合并审批通道）、
ADR-040（GitHub 评论审批）

## 背景

A1 部署按所有者授权把 `GATEWAY_ALLOW_INSECURE` 设为 `0`、`WEBHOOK_AUTH_TOKEN`
设为非空后，GitHub webhook（`POST /webhook/github/delivery`）返回 401。实测诊断：

- 仅带正确的 `X-Hub-Signature-256` 的 ping → 401；
- 同一请求额外带 `X-Gateway-Token: <WEBHOOK_AUTH_TOKEN>` → 200 `{"status":"ignored"}`。

根因：`AuthMiddleware` 对所有 `/webhook/*` 统一要求 `X-Gateway-Token`。而 GitHub
不会发送这个自定义头，只发送 `X-Hub-Signature-256`。两个 GitHub 路由
（`app/routes/github_delivery.py`、`apps/code_review_pipeline/routing/github_review_route.py`）
本身已经对**原始请求体**做 fail-closed 的 HMAC 校验。中间件这层 token 对 GitHub
通道不适用，要求它只会让该通道永远无法投递。

## 决策

在 `AuthMiddleware.dispatch` 中对 `/webhook/github/` 前缀提前放行，不做
`X-Gateway-Token` 校验；请求随后必须通过路由自身的 `verify_signature`。

### 边界

- **精确前缀**：只匹配 `/webhook/github/`（含尾斜杠）。`/webhook/github`
  （无尾斜杠）与 `/webhook/githubx/...` 不在豁免内，仍要求网关 token。
- **不扩大**：`/webhook/{channel}`、`/webhook/callback`、`/feishu/event` 的鉴权
  行为保持不变；没有 GitHub 签名可依赖的路径不能复用这条豁免。
- **不动签名门**：不删除、不弱化 `verify_signature`、`approver_gate_error`、
  `insecure_mode_enabled`；GitHub 两个路由的签名逻辑、合并状态机、推送审批均不变。

### fail-closed 姿态

豁免的是**中间件 token**，不是路由签名：

- `GITHUB_WEBHOOK_SECRET` 非空时，缺失/格式错/算错的签名一律 401；
- `GITHUB_WEBHOOK_SECRET` 为空且 `GATEWAY_ALLOW_INSECURE=0` 时，路由返回 401
  （`auth_not_configured`），豁免不会变成匿名开门；
- 响应体只回笼统的 `{"error": "unauthorized"}`，不区分“缺头”与“算错”，
  避免给调用方一个免费的区分 oracle。

## 备选方案

1. **让 GitHub 也带 `X-Gateway-Token`**：GitHub webhook 配置不支持自定义请求头，
   不可行。
2. **中间件识别 `X-Hub-Signature-256` 并自行验签**：会把 GitHub 签名逻辑复制到
   中间件，形成第二套实现；且中间件无法像路由一样按事件语义与日志记录拒绝原因。
   签名校验留在路由内是单一事实源。
3. **继续要求 `GATEWAY_ALLOW_INSECURE=1`**：这是关闭 token 门的全局逃生口，
   会把 `/webhook/*` 与 dashboard 一并放宽，属于用降低整体安全姿态换取单通道可用。

## 后果

- GitHub webhook 在 `GATEWAY_ALLOW_INSECURE=0` 下可正常投递，同时仍由 HMAC
  fail-closed 把关；
- 中间件注释明确“provider-verified path”语义，防止未来把豁免误扩为 `/webhook/*`；
- 新增回归测试覆盖：有效签名无 token 放行、无效/缺失签名 401、secret 为空 401、
  非 GitHub `/webhook/test` 仍要求 token、伪造前缀（无尾斜杠 / `githubx`）不放行。
