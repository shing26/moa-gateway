# ADR-0010: 请求生命周期状态补全、执行期重试与评估器刹车

日期：2026-09-22
状态：已实施
关联：ADR-008（LangGraph 第二引擎）；ADR-009（统一错误契约）；`delivery/定位与减法清单.md` 自评第 5 行

## 背景

对 12 条 Agent 评测标准逐条核代码时确认了三处"声明存在、运行时不成立"：

1. **状态机有一半是装饰**。`app/fsm/state_machine.py` 定义了 8 个状态，但 `TASK_STARTED`
   之类的"开始执行"事件根本不存在，普通消息路径的 FSM 停在 `ROUTED` 就再不动了——
   agent 执行、评估、guard、送达全程不推进状态机。于是 `EXECUTING` 只有一条入边
   （`SUSPENDED + HUMAN_APPROVED`），`RETRY` / `OUTPUT_READY` / `COMPLETED` 没有任何
   发出者。`TASK_FAILED` 一旦真的发出就是非法迁移（`ROUTED + TASK_FAILED` 不在表里），
   经 `Engine.handle_event` re-raise 变成 500。**"FSM 8 态 + 重试"当时只有 5 态可达。**
   `StateContext.retry_count` 同样永不递增（且每次事件都重建、连持久化都没有）。

2. **评估器要求人工是个哑标志**。`RuleEvaluator` 会算出 `need_human_review`，但
   `pipeline.py` 只把它塞进响应体。真正触发 HITL 的只有 `guard_action == review`，所以一个
   `empty_output`（score 0.0）却没命中策略规则的输出会以 `status="ok"` 正常送达用户。
   评估器想升级人工的意图没有接线到刹车。

3. **图路径的评估审计是断的**。`_node_evaluate` 只把 `eval_score` 存进 GraphState，
   `issues` 被丢弃；`PipelineResult` 也没有承载它的字段，`dispatcher._log_request` 从不传
   `eval_score`——图路径审计里这两个字段永远是 `None`。

## 决策

1. **生命周期补全**：新增 `TASK_STARTED` / `DELIVERED` 事件与执行期迁移，成功路径真的走完
   `ROUTED →(TASK_STARTED) EXECUTING →(TASK_SUCCESS) OUTPUT_READY →(DELIVERED) COMPLETED`；
   guard / 评估器在交付前刹车走 `OUTPUT_READY →(NEEDS_HUMAN) SUSPENDED`。`SUSPENDED` 上补
   `TASK_STARTED` 入边——挂起中再来一条消息仍会执行（同会话可累积多个待审批，按 trace 区分），
   这是既有行为，不补这条边新消息会撞非法迁移。新增
   `test_every_state_is_reachable` 做守卫：8 个状态必须都有真实入边，那种"装饰状态"不能再悄悄回来。

2. **重试预算由状态机结构决定**。`app/agents/retry.py` 的 `RETRY_BUDGET = 1` 与转移表是同一件
   事的两面：表里 `EXECUTING →(TASK_FAILED) RETRY →(TASK_FAILED) SUSPENDED` 只允许一跳。
   预算**不是配置项**——改预算就是改表，`test_retry_budget_matches_the_state_machine_structure`
   把两者钉在一起。重试期间状态停在 `RETRY`（不回到 `EXECUTING`），否则第二次失败会落在
   `EXECUTING → RETRY` 而不是 `→ SUSPENDED`，预算就编码不进表里。

3. **重试是共享纯函数，不是图节点**。`execute_with_retry` 无状态，做成模块级函数而不是注入的
   协作者：不必改 `MoAPipeline.__init__`，也就不必同步 `test_langgraph_adapter.py` 那条
   "协作者表面 == MODELLED ∪ OUT_OF_SCOPE"的漂移守卫。两条引擎在自己的 execute 阶段都调它，
   所以图**不需要加节点或回边**，`node_path` 的精确断言不变。

4. **归因喂回，成本不丢**。失败原因经 `AgentEnvelope.failure_reason` 传给下一次尝试
   （frozen dataclass 用 `dataclasses.replace` 生成新 envelope，`agent_local_slot` 仍是同一个
   可变 dict）；agent 只把它拼进自己的 prompt，**不改 `user_raw_input`**——那是审计与长期记忆
   的原始输入。失败尝试烧掉的 token/成本由 `_absorb_metrics` 跨尝试累加，否则重试会让审计里的
   成本系统性偏低。`asyncio.CancelledError` 原样上抛，绝不重试。

5. **工具级失败不重试**。ReAct 循环把工具异常转成 observation 让模型下一轮自愈
   （`app/agent_core/react.py`），那是更细粒度的恢复；盲目重试有副作用的工具（写笔记、发消息）
   本身是危险的。所以恢复是四层的：工具级自愈 → 步骤级（`AGENT_MAX_STEPS`）→ 请求级重试
   （本 ADR）→ 人工接管。只有**上抛到 pipeline 的基础设施异常**（LLM 超时/网络/解析失败）
   才触发请求级重试。

6. **重试耗尽 → 升级人工，而不是回一句 error**。复用 guard REVIEW 的同一套闭环
   （`store_hitl` → `NEEDS_HUMAN` → 卡片 → 回调 approve/reject → 审计）。`HitlRequest.hitl_kind`
   区分来源（`review` / `eval_review` / `failure_escalation`），卡片按来源切换标题、配色与输出
   标签——失败升级卡片里没有"待批准的输出"，沿用审批标题会让人误以为有内容要批。`hitl_kind`
   也写进回调侧审计，人工决策因此可按来源拆分回流。审批开关关闭时没有人可升级，退回原来的错误
   返回语义。

   **两个引擎必须读同一个开关**。此前 graph 的 `_hitl_enabled()` 回退到 `MOA_HITL_ENABLED`
   （默认 true），而管线读 `app.config.settings.hitl_enabled`（env 名 `HITL_ENABLED`，默认 false）
   ——同一个概念两个变量名、两个默认值，且 `MOA_HITL_ENABLED` 全仓库只有那一行在用。任何只设了
   其中一个的部署都会让两条引擎对"要不要人工审批"给出相反答案。现统一到 `settings.hitl_enabled`，
   并有 `test_golden_failure_with_hitl_disabled_matches` 钉住关闭态下两引擎同为 `error`。

   顺带修掉一个既存的图路径 bug：`_after_execute` 原先没有 error 分支，execute 节点返回的
   `status="error"` 会顺着 `ok` 边继续走 evaluate → guard → deliver，于是**图路径的 agent 崩溃
   会被当成正常回答交付**（空输出经 guard 后 deliver，`status` 被覆盖成 `"ok"`）。FSM 路径没有
   这个问题——它的错误分支直接 return。现在 execute 的错误走新的 `failed` 边直接到 `END`。

7. **评估器接刹车**。`_merge_guard` 的第三个参数从**从未被使用**的 `policy_ids` 换成
   `eval_verdict`（同 arity，最小 diff），优先级 `DENY > REVIEW`。新增
   `_verdict_from_eval_issues` 做分级：AST 危险类 issue（`dangerous_*` /
   `write_mode_open:*`）→ **DENY**（可执行危险代码不是"要不要批准"的问题）；其余 issue
   （空输出/超长/非法 JSON/未完成标记）→ **REVIEW**（进人工）。两条引擎共用同一份定义与同一条
   合并规则（`adapter_test` 钉住 `graph_module._merge_guard is _merge_guard`）。

8. **审计 `guard_action` 沿用既有字面量**。`audit_stats` / `dashboard_html` /
   `collect_hitl_feedback` 三处硬编码只认 `"deny"` / `"review"`，新字面量会从聚合里静默消失。
   所以来源改用新增的 `hitl_kind` 表达（`eval_review` / `eval_deny` /
   `failure_escalation`），guard 触发时留空以保持既有语义。同时给 `PipelineResult` 补
   `eval_score` / `eval_issues` / `retry_count` / `retry_reason` / `hitl_kind`，由 dispatcher
   透传，图路径的评估审计断链随之修复（`None` 仍表示"没跑评测"，与 `0.0` 区分）。

## 后果

- 全量测试 670 → 726 passed / 8 skipped（**在 `HITL_ENABLED` 开与关两种条件下都跑过**——首轮提交
  只在开关为 true 的本机验证，CI 因开关默认 false 当场红了 4 条用例：升级路径依赖该开关，而用例
  没把它定死）；`ruff`、`bandit` 干净；红队 200 条仍 100% / 0 误拦；
  `eval --offline` 退出码 0。
- **对外契约变化**：成功响应的 `state` 从 `ROUTED` 变为 `COMPLETED`，被拦截的输出停在
  `OUTPUT_READY`（不借用 `REJECTED`——那个状态专指"人工拒绝"）。受影响的断言已同步
  （`test_langgraph_adapter` 的参数化期望、`test_pipeline` 的 ok/deny 路径、golden 场景）。
- **行为变化**：`empty_output` / `output_too_long` 这类原本能正常返回的输出现在会进人工审批。
  这是语义正确的代价；若演示体验优先，可把 `empty_output` 从 REVIEW 排除（改成只重试）。
- **resume 路径的边界**：`resume()` 刻意不驱动 Engine（审批回调才是那条路径的推进者），所以
  审批续跑时图自己的 `fsm_state` 会领先于会话真身。`_advance_execute` 用 `_engine_matches`
  只在两者一致时才推进 Engine——两个写者不能抢同一个会话，否则会撞
  `SUSPENDED + TASK_SUCCESS`。
- **审批放行的交付不推进 Engine**（FSM 路径）：交付在 `app/routes/feishu.py`，会话因此停在
  `EXECUTING` 直到下一条消息。图路径的 deliver 节点会推进到 `COMPLETED`。这是既有结构差异
  （图不负责发卡片、路由负责 FSM 的送达），本轮不动路由层。
- **失败路径不进 e2e 数据集**：`guard_service.evaluate_output` 检查的是 **agent 输出**而不是
  用户输入，活体路径下模型输出不可控，所以"输入含内网 IP 就必然 blocked"并不成立——那样加
  进去只是 flaky 的门禁数据。失败路径的可复现覆盖在单测里（`test_agent_retry.py`、
  `test_pipeline.py` 的升级/刹车用例、golden 的失败升级场景），`test_eval_metrics` 另有一条
  断言钉住 harness 表达"期望非 ok 状态"的能力。
- `Event.FALLBACK_APPLIED` 仍是无迁移无发送者的保留枚举（原意是 provider 级 fallback 信号），
  本轮未动。
