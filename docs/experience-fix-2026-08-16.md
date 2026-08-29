# 体验评估报告修复记录（2026-08-16）

本记录对应仓库根目录 `moa-gateway-体验评估报告.md` 的问题清单。修复全部停留在
`fix/experience-report` 分支，按功能拆成 12 个提交，每个逻辑变更都配有
`tests/unit/` 测试。

## 修复状态

| 编号 | 结论 | 处理方式 |
|---|---|---|
| B1 | 已修复（代码层） | `_build_kwargs` 现在按 `LLM_PROVIDER` 解析 LiteLLM provider：direct/omniroute 传 `custom_llm_provider=openai`，openrouter/deepseek 等显式 provider 自动补前缀，local 走 `ollama/` 前缀；`.env.template` 补 `LLM_PROVIDER`。真实请求仍依赖用户环境提供可达的 Base URL 与匹配的 Key。 |
| H1 | 已修复 | 前端保存时空 Key 发送 `null`，后端对空字符串不再覆盖运行时 `OPENAI_API_KEY`，统一"留空则保持当前 Key"。 |
| H2 | 已修复 | `init_prompts()` 注册 `review` prompt；`select_canary_version` 对未知 agent 回退 general；pipeline 对 canary 选择加兜底，不再 500。 |
| H3 | 已修复 | `/webhook/github/review` 在无 `GITHUB_TOKEN` 时返回 200 `status=degraded` + 降级文案，与 README 一致；聊天路径原有降级不再被上游 500 抢先。 |
| M1 | 已修复 | `SessionStore` 持久化 FSM 状态，`Engine.handle_event` 跨事件读取/保存状态；pipeline 消费状态：`cancel/reset` 重置会话（清状态、记忆、模式），敏感词消息挂起返回 `pending_review` 且不执行 agent；HITL 回调兼容 ROUTED→SUSPENDED→EXECUTING/REJECTED。 |
| M2 | 已修复/收敛 | `IntentRouter` 支持可选微模型/路由 LLM：设置 `MICRO_LLM_MODEL` / `ROUTER_LLM_MODEL` 后接线，Key/Base URL/Provider 继承 `LLM_*`；README 改为如实描述"默认纯正则，配置后启用升级"。`execute_code` 工具从注册表移除（无真实审批执行语义），README 不再宣称 execute_code 强制 REVIEW，改述高危意图 HITL。 |
| M3 | 已修复 | 畸形 JSON 返回 400 `invalid_json`；全局异常处理器只返回通用 500，不再泄露异常类名与详情。 |
| M4 | 已修复 | 前端 `fetchJSON` 解析服务端 `detail`/`message`/`error` 并展示，不再只显示状态码；Flag 还原也复用该逻辑。 |
| L1 | 已修复 | 侧边栏根据 `DASHBOARD_PASSWORD` / `WEBHOOK_AUTH_TOKEN` 动态显示鉴权状态。 |
| L2 | 已修复 | 详情页删除文档先展示 toast 再跳转。 |
| L3 | 已修复 | Obsidian 未启用时同步接口返回 400 + "Obsidian 未启用"，不再伪造"0 篇变更"。 |
| L4 | 已修复 | Flag API 对默认布尔/整数开关做类型校验，`canary.enabled=50` 等非法值返回 400。 |
| L5 | 已修复 | 移除 `guard_service.evaluate` 每次请求的 warning 调试行；Ops 页运行状态按真实值渲染 success/warn/danger/neutral 圆点。 |
| P1 | 已收敛 | `/healthz` 服务端加 2s 缓存；概览页轮询失败指数退避（5s→60s），成功后恢复。 |
| P2 | 已收敛 | 测试台请求加 20s 前端超时与"请求超时"提示，不再无限等待。 |
| P3 | 已收敛 | Ops 测试连接前显示"正在连接 LLM，失败时将尝试备用模型"的进度文案。 |

## 未修项（环境/范围）

- B1 的端到端验证依赖用户 `.env` 中的 OmniRoute/DeepSeek 可达性，代码层 Provider
  接线已修复；报告中的"机器无可用 LLM key"环境限制仍存在。
- HITL 飞书卡片完整闭环（真实飞书发送、审批后执行）依赖 FEISHU 配置，与报告一致
  未做真实网络验证；FSM 回调状态迁移已补测试。
- 三级路由的 LLM 升级默认不启用（避免无 Key 时发起无效调用），README 已如实收敛。

## 验收（2026-08-29 复测）

- `pytest tests/unit -q`：428 passed / 9 skipped
- `ruff check .`：通过
- `bandit -r app -q`：通过（清理型 except/空默认值加 `# nosec`，Obsidian doc id 改
  `usedforsecurity=False`）
- `python evals/run_evals.py --offline`：intent accuracy 1.0，guard deny recall 1.0，
  e2e 30 条离线跳过
