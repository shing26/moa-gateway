# ADR-0009: 统一错误契约、配置 fail-fast 与 per-session 预算拦截

日期：2026-09-19
状态：已实施
关联：ADR-008（LangGraph 第二引擎）；2026-09-19《全项目框架补强建议》M1/M2/M5/M6 与实测新发现 N1-N5

## 背景

补强评估实测确认的问题：全仓无错误码枚举（三套状态词汇表并存，LangGraph 路径的错误细节在
`_to_result` 处丢失）；配置"配置了但非法"的值除两个超时外全部静默回默认（其中 embedding
维度配错=检索结果全错且无告警）；供应商清单在 provider 层与 dashboard 表单各硬编码一份且已
实际漂移；成本只核算不限额，且 TaskAgent 路径的成本从未进审计、ES writer 丢弃 cost_usd；
dashboard.py 1053 行混装 HTML/模型/路由；agents/agent_core 反向 import 组合根 `app.deps`。

## 决策

1. **错误契约（M1/N1/N2）**：`app/models/errors.py` 提供 `ErrorCode` 枚举（取值与既有线上
   字面量一致，不破坏外部契约）+ `MoaError` + `http_status_for`。`PipelineResult` 增加
   `error_code`；双引擎错误出口都产出 code（`error_code` 加入 parity 的 `COMPARED_FIELDS`，
   引擎间不一致会打红 CI）。webhook/chat/dashboard 统一 `{"error": code, "message"}` 形状；
   chat 的 `status=error` 不再伪装成 200。**飞书回调保持一律 200**——平台契约会重试非 200
   响应，错误只体现在回复文案与日志。
2. **配置 fail-fast（M2/N5）**：`Settings.validate()` 在 `__init__` 尾部（即 import 期、
   deps 构造单例之前）执行。只拦截"配置了但非法"：维度非正整数、超时 ≤0、池 min>max、端口
   越界、预算为负。**空值一律视为未配置**——tests/conftest 的"置空即屏蔽"与本项目对缺失配置
   的 forgiving 哲学是红线。次要数值项非法时 `logger.warning` 一次。`ENGINE` 未知值保持
   告警回退（降级语义已在双引擎章节文档化，不 fail-fast）。
3. **供应商注册表（M5）**：`app/agents/provider_registry.py` 的 `PROVIDERS` 单表派生
   `_qualify_model` 的前缀映射与 ops 表单 `<option>`，消除两处漂移硬编码。
4. **dashboard 拆分（M3，方案 B）**：HTML 模板 → `app/rendering/`，请求模型 →
   `app/schemas/`，审计聚合 → `app/services/audit_stats.py`；路由文件 1053→350 行、无 HTML
   字面量。为 test_security_stats 保留 4 个 re-export 名字作为兼容契约。
5. **反向依赖（M4）**：`app/knowledge_access.py` 中立 port，组合根 `configure()` 注入；
   领域层不再 import `app.deps`，并以 `tests/unit/test_layering.py` 的 grep 断言进 CI。
   Protocol 注入留作升级路径（工具 handler 签名受 LLM 参数约束，本轮不值得动）。
6. **预算拦截（M6）**：`app/budget/guard.py` 进程内 per-session 累计器；
   `BUDGET_SESSION_LIMIT_USD<=0`（默认）只核算不拦截，请求路径零变化；>0 时超限会话的
   **下一次**请求在 agent 执行前被拒（成本只能在调用后得知）。拒绝形状：`status=blocked` +
   `error_code=budget_exceeded` + 审计 `guard_action=budget_exceeded`。双引擎共用 deps 构造的
   同一实例，LangGraph 侧经条件边短路到 `blocked` 节点，并登记进 adapter 的
   `MODELLED_COLLABORATORS` 防漂移门禁。
7. **成本核算补漏（N3/N4）**：TaskAgent 在 plan/ReAct/summarize 各阶段累加 `last_metrics`
   写回 `llm_metrics` 通道（此前 ReAct 成本为 0）；ES writer 的 bulk body 补齐 WAL 同款成本字段。

## 后果

- 全量测试 632 → 670 passed / 8 skipped / 0 warning；每项改动独立提交，逐项跑门禁。
- 多实例部署的两个已知边界显式化：预算累计器与 FSM 会话状态同为进程内（README"已知边界"）。
- 新增错误码只改 `ErrorCode` + `HTTP_STATUS`；新增供应商只改 `PROVIDERS` 单表；
  新增预算变量走同一 `_parse_float + validate` 模式。
- 遗留：redis_state 接线（README:194 简历口径）按 2026-09-19 决策不在本轮，需单独立项。
