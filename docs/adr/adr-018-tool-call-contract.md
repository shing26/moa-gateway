# ADR-0018: 工具调用契约与 task-LLM 降级可见性

日期：2026-09-28
状态：已实施（决策 1–7，2026-09-28）
关联：ADR-011（降级可见性，本 ADR 是其**未覆盖的那条路径**）、ADR-016（台账，本项属表外新发现立项）、
`CONTEXT.md`（治理对象 = 模型输出）

## 背景

2026-09-27 的外部七维审计（v2）对 moa-gateway 的 D5 判定为 ⚠️，理由是"工具参数零校验"。
复核时往下读一层，发现两处**比"零校验"更硬**的问题，以及一处审计说浅了的表述。

1. **模型可自选 `session_id`，越过会话边界。** `app/agent_core/react.py:85-88`：

   ```python
   args = dict(decision.arguments)
   if "session_id" in tool.parameters.get("properties", {}):
       args.setdefault("session_id", self._session_id)   # ← setdefault
   ```

   `setdefault` 只在模型**没给**时才用系统值。而 `app/agent_core/tools_extra.py:82` 的
   `_NOTES_STORE` 是模块级 dict、以 `session_id` 为 key（`:85-105` 的 `add_note`/`get_notes`）。
   合起来：模型自带一个 `session_id` 即可读写**别的会话**的笔记。工具零校验是"可能崩"，
   这条是"能越界"。

2. **工具参数 schema 从未进入模型上下文。** `app/agent_core/litellm_llm.py:93-95` 拼 decide
   提示词时只取 `t['function']['name']` 与 `description`，`parameters` 整个丢掉。所以模型在
   **猜**参数名与取值。这决定了"加校验"不能单独做——只加校验会把毛病从"参数错→执行崩"
   换成"参数被拒→模型仍不知道该怎么填"。

3. **task-LLM 崩溃被折叠成正常回答交付。** `app/agent_core/litellm_llm.py:42-47` 的 `decide`
   捕获一切异常后返回 `ReActDecision(action="finish", final_answer=f"决策失败: {exc}")`，
   经 `react.py` 后以 `status=ok` 交付。`plan` / `summarize` / 畸形 JSON 同类。
   **注意：ADR-011 修的是 graph 路径的降级可见性，这条 task-LLM 路径当时漏了**——
   "修一半 = 没修"。

4. **`calculator` 无界且阻塞事件循环。** `app/agent_core/tools_extra.py` 的 `_ALLOWED_OPS`
   含 `ast.Pow`（`:22`），`_eval_node`（`:27-40`）递归求值，无表达式长度上限、无指数上界；
   而 `_calculator_handler` 是 `async def` 却做纯 CPU 计算（`:43-52`）——`9**9**9` 会
   **同步阻塞事件循环**。

审计的一处表述需要修正（不改变结论，但改口径时别按错的说法改）：工具失败**并未**被吞成
`tool_errors`——失败其实回流成了 observation（`react.py:96-99`），模型能看到。真问题是
**"被拒"与"执行失败"共用一个计数器**，指标分辨不出"模型输错了"与"环境挂了"。

## 决策

1. **参数校验用 `jsonschema`，并把它提升为直接依赖。**
   `tool.parameters` 本就是合法 JSON Schema（`app/agents/tools.py:11-20` 的 `AgentTool`）。
   校验在 `react.py` 调用 handler **之前**执行。
   刻意选择"提升为直接依赖"而非"直接 import 传递依赖"：`jsonschema` 当前**只由 litellm 传递
   提供**（`uv.lock` 内，4.26.0），代码隐式依赖第三方传递依赖会在对方换树时静默碎——这与本仓
   "单一来源 / 诚实口径"（ADR-015、ADR-016）相背。提升为直接依赖是 pyproject 一行、
   **零新增解析成本**。
   另记：`pydantic` 虽已是直接依赖，但工具 schema 是裸 dict 而非 pydantic model，
   `TypeAdapter` 路线不适用，故不采用。
2. **`session_id` 改覆盖式注入**，不再 `setdefault`：系统值与模型值冲突时**系统值胜出**。
   未声明 `session_id` 的工具不受影响。
3. **"校验拒绝"与"执行失败"分离计数。** 新增独立的拒绝计数（不并入 `tool_errors`），
   校验失败的**错误原文**回填 observation，让模型自纠。指标上两者必须可分——这是本轮
   修正原表述后的直接要求。
4. **工具 `parameters` 渲染进 decide 提示词**，与决策 1 同轮落地。
5. **`calculator` 加界并移出事件循环**：指数/操作数上界 + 表达式长度上限，求值移入
   `asyncio.to_thread`。
6. **写风险用规则治理，不引入"工具操作审批"。** 治理对象仍是**模型输出**（`CONTEXT.md`：
   "操作、变更、工单……那些是'操作审批'的对象，本项目不治理"）。工具写风险靠
   **校验 + 有界轮次（`AGENT_MAX_STEPS`）+ 默认只读**的确定性规则约束。
   当前 6 个工具中真正"写"的只有 `add_note`/`get_notes`，写的是进程内 `_NOTES_STORE`
   （重启即丢、不落库、不改业务状态）——为它建审批闸门是过度工程。
   **触发线**：出现**真正持久化/有外部副作用**的工具（落库、发消息、动钱）时重新立项，
   届时建"副作用分类 + 默认拒绝的允许表"（已登记进 ADR-016 台账）。
7. **task-LLM 异常不得折叠成正常回答**（关联 ADR-011）：`litellm_llm.py` 的
   `decide`/`plan`/`summarize` 捕获异常后**不得**返回一个外观正常的 `finish`。
   `app/agent_core/types.py` 增加 `degraded` 标记，`task_agent.py` 写入对应 slot，
   `app/pipeline.py` 按既有 `_tool_failure_issues` 的模式接 REVIEW。两个引擎同改。

## 验收

1. 模型自带越界 `session_id` 被系统值覆盖；未声明该参数的工具行为不变。
2. 非法参数 → 拒绝计数上升、**执行失败计数不动**、模型收到校验错误原文。
3. `9**9**9` 被上界/长度上限拒绝而非挂起；calculator 不再阻塞事件循环。
4. 注入一个必然抛错的 task-LLM → 终态为 degraded / REVIEW，**绝不出现 `status=ok`**。
5. `jsonschema` 出现在 `pyproject.toml` 直接依赖；ruff、bandit、全量测试零回归。

## 实施记录（2026-09-28）

决策 1–5 已落地。实施中有一处相对决策 1 的**扩大**，必须记录：

- **契约落在共享模块 `app/agents/tool_contract.py`，不是只补 `react.py`。**
  动手前发现工具调用有**两条**互不相干的路径：
  - `app/agent_core/react.py` 的 `ReActLoop`（本 ADR 背景里引的那条）；
  - `app/agents/stubs.py` 的 `_execute_with_tools`（`coder` / `general` 两个 agent 走这条），
    它**既不校验也不注入 `session_id`**——模型自带的 `session_id` 原样落进
    `handler(**arguments)`，因此这条路径的越权比 `react.py` **更直接**（后者至少还有
    一个系统值，只是被模型压过）。

  只补一条路径等于交出一个只有一半的契约，所以两条都改为走
  `tool_contract.call_tool()`。**副作用**：`add_note` / `get_notes` 在 coder/general
  路径上从"永远返回『缺少会话标识』"变为可用——这是修复不是回归，但是一条可观察的
  行为变化，记在此处备查。

- **`session_id` 注入判据**：仅当工具 schema **声明**了该参数时注入，且用覆盖式赋值。
  未声明的工具若收到 `session_id`，按"未声明参数"拒绝。

- **拒绝计数已进审计。** `TaskResult.tool_arg_rejections` →
  `agent_local_slot["tool_arg_rejections_total"]` → `PipelineResult.tool_arg_rejections` →
  `log_request(tool_arg_rejections=...)` → 审计 extra。图引擎同步接线（`graph.py` 的状态字段
  与 `dispatch.py` 的映射），两条引擎字段集合一致；`tests/unit/test_audit_field_coverage.py`
  的守卫把它钉住（与 `tool_calls`/`tool_errors` 同为"有工具活动才写"的可选字段）。

- **calculator 只限指数是不够的**：`(9**64)**64` 的指数是 64，但结果是 `9**4096`。
  所以按"预测结果位数"再拦一道（`_MAX_RESULT_BITS`）。

- 验收实测：`pytest tests/unit` **829 passed**（基线 799 + 新增 30 = `test_tool_contract.py` 15
  条 + `test_task_degradation.py` 8 条 + `test_react_loop.py` 3 条 + `test_pipeline.py` 3 条 +
  `test_request_logger.py` 1 条）；ruff / bandit / `check_no_source_patch` / 红队 /
  `run_evals.py --offline` 全部与基线逐字一致（intent 1.0 / guard 1.0 / tool_select 1.0）。

**决策 7（降级可见性）已实施（同日）**。`litellm_llm.py` 的 `decide` / `plan` / `summarize`
都不再产出"外观正常的兜底结果"，而是**带上降级原因**：

- `ReActDecision.degraded_reason`：`decide` 调用失败、以及**模型输出不是合法 JSON**
  （此前它被直接当成"收尾答案"交付）都置位；
- `TaskLLM.plan` / `summarize` 增加**可选**的 `on_degrade` 回调（缺省 `None`），
  `LiteLLMTaskLLM` 在异常与非数组输出时上报；
- `TaskResult.degraded_reasons` 由 `ReActLoop` 汇总，`TaskAgent` 每请求一个收集器
  （**不能挂在 `self._llm` 上**——它是进程内单例，并发请求会互相踩）写进
  `agent_local_slot["task_degradation_reasons"]`（只在真降级时才写，缺席有含义）；
- `pipeline.py` 新增 `_task_degradation_issues()`，与 `_tool_failure_issues` 并排接进同一条
  评估器刹车 → REVIEW → 人工；**图引擎同步**（`graph.py` 的状态键与同一个函数）。

降级原因走"返回值 + 可选回调"而不是让 LLM 自己记事，是这次实现里唯一非显然的取舍：
`TaskAgent.__init__` 的 `self._llm` 只在模块导入时构造一次，被所有请求共享；把降级状态
挂在它上面会让 A 请求的降级串进 B 请求的判定。测试
`test_degradation_is_per_request_not_on_the_llm` 钉住这一点。

**关于 e2e 基线**：此前担心决策 7 会改变部分 e2e 用例的 `expected.status`，实测**未发生**——
离线 `run_evals.py --offline` 与基线逐字一致（e2e 用 `MockTaskLLM`，它不会降级）。
差异只在真实 LLM 路径上出现：那边降级本就会发生，只是以前被伪装成 `ok`。
所以**没有需要校正的基线**，这条担心可以销掉。

## 过程发现与修正（2026-09-28，同日已修）

发现时为**本轮范围之外**的一个既有缺陷，但与本 ADR 的计数器同源，故同日一并修掉。

`app/pipeline.py` 的 `_tool_failure_issues` 声称"工具调用**全部失败**时刹车转人工"，判据是
`tool_calls > 0 and tool_errors == tool_calls`。而 `react.py` 的 `tool_calls` 只在 handler
**成功返回之后**自增——它是**成功数**，不是**尝试数**。两者相乘的后果正好相反：

- 真·全失败（`tool_calls=0, tool_errors=3`）→ `tool_calls > 0` 为假 → **不刹车**；
- 部分成功（`tool_calls=2, tool_errors=2`）→ 相等 → **刹车**。

也就是说 `README.md` 声称被拦下的那个场景（"所有工具都失败、任务却照样返回了结果"）
**从未被拦下**，被拦下的是另一种情形。既有测试 `test_all_tool_calls_failed_brakes_into_hitl`
喂 `2/2` 却把名字写成"全失败"，恰好把这个错误语义固化住了——**测试名描述的是意图，
数据描述的是实现，两者不一致时这轮测试就成了缺陷的看门人**。

**修法**：把 `tool_calls` 的口径定成**尝试数**。依据是 `types.py` 的字段注释、README、
以及那个测试名本来就都按尝试数写，**实现是唯一说反的一方**。于是：

- `react.py` 在 `call_tool` 分支**入口**自增 `tool_calls`（成功、失败、被拒都算一次尝试）；
- 判据变为 `tool_calls > 0 and tool_errors + tool_arg_rejections == tool_calls`，
  即"有尝试且一次都没成功"；**被拒计入"没成功"**——那是模型连着填错参数，同样不该把
  靠瞎猜拼出来的结果当正常交付；
- README 与 `types.py` / `PipelineResult` 的注释同步改成尝试数口径；
- `react.py` 三处测试的预期从"成功数"改成"尝试数"，并补两条：**全被拒要刹车**、
  **没有工具活动不刹车**（守住判据里的 `tool_calls > 0`）。

**取舍**：这改了 `tool_calls` 这个**已审计字段的语义**（成功数 → 尝试数）。选它而不是反过来
改判据，是因为声明的一方三处一致、实现是孤例；而且"成功数"这个口径此前**没有任何地方声明过**，
它只是实现的一个副产品。改一处实现比改三处声明更诚实，也更符合 ADR-015 的方向。

## 后果

- 新增直接依赖 `jsonschema`（已在锁定树内，零新增解析成本），消除一处隐藏的传递依赖耦合。
- 审计产物与评测指标新增字段（`tool_arg_rejections`），`evals/reports/latest.json` 的口径需同步。
- **`tool_calls` 的语义从"成功数"改为"尝试数"**（2026-09-28）。任何读这个字段做判断的
  下游都要按尝试数理解；成功数 = `tool_calls - tool_errors - tool_arg_rejections`。
  这是本 ADR 最容易被漏的回归面。
- **task-LLM 降级现在会转人工**：真实 LLM 抖动不再以 `status=ok` 静默交付，代价是
  用户看到的人工介入会比以前多——那是**可见性提升**而不是质量下降，但会体现在
  `human_intervention_rate` 上，看这个指标时要记得这条口径变化。
- "写风险不设审批"是**定位性决定**，与 `CONTEXT.md` 的治理对象一致；它保护的是项目的
  唯一性，代价是当前**没有**任何工具操作的审批能力——这个"能力缺口"是刻意的。
- 决策 1–7 已全部实施；原先"MUST 先校正 e2e 基线"的担心**实测不成立**
  （详见上文"关于 e2e 基线"）。delta 门禁本身仍留在 ADR-016 台账，与本 ADR 无关。
