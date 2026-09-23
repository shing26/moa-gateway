# ADR-0013: HITL 回调的可信度（验签真正生效、单次决策、批准人留痕）

日期：2026-09-23
状态：已实施
关联：ADR-010（生命周期与重试）；ADR-012（配置单一来源与 HITL 失效语义）；`feishu_signature.py`、`feishu.py`、`webhook.py`

## 背景

治理闭环是本项目**唯一**成立的一条闭环（消息→路由→守卫→审批→送达→审计→决策回流评测），
也是简历上最强的卖点。但它的门是开着的，三处：

1. **验签没生效**。`verify_verification_token` 只在**顶层** `body["token"]` 找凭据，而
   **v2 事件把 verification token 放在 `header.token`**（`parse_feishu_event` 也是按
   `header.event_id` 判 v2 的）。所以配了 `FEISHU_VERIFICATION_TOKEN` 的部署，
   在真实回调上**从未真正比对过**。
2. **无并发幂等**。回调是"读 `get_hitl` → 判断 → 删 `remove_hitl`"三段，中间有窗口：
   卡片被连点两次、或两个回调并发到达时，两个请求都能通过 → **重复送达 + 两条审计**。
3. **答不出"谁批准的"**。`parse_feishu_event` 对 v2 卡片事件只取 chat_id 与 action value，
   不取 `operator`；审计只记 `guard_action=hitl_approve`。审批单据的基本字段缺一半。

## 决策

1. **验签改为 fail-closed，与 `AuthMiddleware` 同一套策略。**
   配了 token：v1 顶层 / v2 `header.token` 必须有且常量时间相等，**取不到即拒绝**；
   没配：仅当显式 `GATEWAY_ALLOW_INSECURE=1` 时放行，否则 401。拒绝时打点记录
   `has_top_token / has_header_token / configured`，便于现网定位"平台实际带的字段位置
   与我实现的不一致"。

   **必须说清：这是在推翻一个刻意的、且有测试钉住的决策。** 此前
   `test_fail_open_when_not_configured` 与 `test_schema_2_0_token_lives_in_header_not_body`
   明确断言 v2 取不到 token 时放行。翻转的理由不是"更严格更好看"，而是后果：
   **知道 session/trace 的任何人都能 POST 一个审批，批准一条真实业务的输出**。
   两条旧用例已按新契约重写，并在文件头注明翻转原因。

2. **单次决策：原子认领（`pop_hitl`）。**
   `RedisHitlStorage.pop` 优先用服务端 `GETDEL`（Redis 6.2+ 原语，一次往返），老服务端退到
   Lua（GET+DEL 同脚本，同样原子），无 Redis 走内存 pop（事件循环内天然原子）。
   两个回调入口都从"get 之后再 remove"改为**认领即删除**：第二个调用者拿到 None，
   回"该审批请求已失效或已被处理"，既不送达也不写决策审计。

   **代价明确**：认领之后若本地格式化或发送失败，该记录不会回来（不可重试）。
   取这一头是因为**重复送达是更坏的语义**——此前注释声称"发送失败可重试"，而实现是
   `remove_hitl` 先于 `_safe_send`，两者本就相反；现在两处一致且写明。

3. **批准人留痕。** `parse_feishu_event` 提取 v1 顶层 `open_id` / v2 `event.operator.open_id`，
   经 `log_request(hitl_operator=...)` 落审计（仅非空时写入）。
   ⚠️ **v2 的 `event.operator` 字段路径是按 schema 2.0 写的，未对着真实卡片点击验证过**
   （只在单测里构造过）；取不到时留空，不影响审批本身。

## 后果

- 测试 758 → **770 passed / 8 skipped**；`ruff`、`bandit -r app` 干净；红队 200 条 100%/0 误拦；
  `eval --offline` 退出码 0；活体 e2e `success 1.0`。
- **自查发现并修掉一个先前提交的隐藏 bug**：`4367ece` 把 `session_state` 改名
  `session_context` 时漏改了 `/webhook/callback` 的**拒签分支**（`NameError` → 500）。
  当时唯一覆盖回调的用例是 `@pytest.mark.skip` 的，所以我新加的失效用例也没碰到它——
  "全绿"又一次掩盖了缺陷。现在补了拒签路径用例。**教训：改名类改动要 grep 旧名残留，
  不能只看测试全绿。**
- **仍然是敞开的**：加密模式（`FEISHU_ENCRYPT_KEY` / `X-Lark-Signature` 的 HMAC 与时间戳
  防重放）未实现，配了它事件会被忽略；回调仍**不校验点击者是否有审批权限**（RBAC 不在
  回调路径上，角色仍来自 `MOA_DEFAULT_ROLE` 环境变量）——现在至少能回答"是谁"，但还不能
  拒绝"不该批的人"。
- **对现网的提示**：`.env` 里若已配 `FEISHU_VERIFICATION_TOKEN`，本次改动会让不携带该 token
  的回调变成 401。若真实飞书回调因此被拒，日志会给出 `has_header_token` 判定；
  本地演示可用 `GATEWAY_ALLOW_INSECURE=1` 显式回到放行（但那时门是开的，需自知）。
