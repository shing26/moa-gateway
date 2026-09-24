# ADR-0015: 诚实收敛——把"已声明为真、实际不成立"的三处修成一致

日期：2026-09-24
状态：已实施
关联：ADR-004（审计落盘）、ADR-012（配置单一来源）、ADR-014（收尾三态）

## 背景

第十轮，主题定为**只做"声明与实现一致"，零新功能**（与 `delivery/定位与减法清单.md` 的
"诚实收敛比继续加功能更有说服力"一致）。

起因是逐行核对 README 的 `## 已知边界`：那份文档非常完整，绝大多数边界都是如实声明
（OTel 是预留接口不是链路、评测没有基线对比且注明"不是遗漏"、PR 审查只读、上下文预算
不计系统提示与工具描述）。但**有三处已经写成"可查"，实际查不到**，另有一处真缺陷被
"测试 0 warning"掩盖。这四件事都不是"缺能力"，而是"仓库对自己说了不准确的话"。

## 决策

1. **审计落盘字段改为单一来源**（`AuditEntry.to_audit_dict()`）。此前 `wal.py` 与
   `es_writer.py` **各自手写一份字段白名单**，于是 `request_logger` 构造的
   `route_fallback` / `tool_calls` / `tool_errors` / `context_budget` / `retry_count` /
   `retry_reason` / `hitl_kind` / `hitl_operator` / `method` / `path` / `hitl_duration_ms` /
   `violation` **在落盘时被静默丢弃**，只留在内存对象里；ES 侧还比 WAL 少
   `policy_hits` / `hitl_decision` / `status` / `duration_ms` / previews，两个 sink 的审计
   数据互相不一致。而 README 已声称 `route_fallback`（第 274 行）、`tool_calls`/`tool_errors`
   （第 308 行）、`context_budget`（第 336 行）可查——**三句都是假的**。
   更早的受害者在 `app/services/audit_stats.py`：它一直在读 `violation` / `hitl_duration_ms`，
   而这两个字段从未落过盘，指标恒为默认值。

   修法：字段集合只由 `to_audit_dict()` 提供（扁平输出，沿用审计 JSONL 的既定约定），
   两个 sink 都从它取。**唯一允许的表示差异**是 WAL 不落 `agent_output` 全文（只留
   `agent_output_len`）以控制日志体积，ES 保留全文以便检索。

2. **新增结构守卫并自测它会红**（`tests/unit/test_audit_field_coverage.py`）。断言：
   每个 `AuditEntry` 字段、每个 `request_logger` 能产出的 extra 键都真的落盘；两个 sink
   字段集合一致；两个 writer 不得再各写一份白名单。**守卫自身被测**——把 `extra` 从
   序列化出口摘掉，3 条用例当场变红（项目方法论⑨："加门禁要自测门禁本身会红"）。

3. **工具轮次 / ReAct 步数上限统一为单一设置**（`AGENT_MAX_STEPS`，默认 **3**）。
   此前同一概念有两个值：`app/agents/stubs.py` 的 `MAX_TOOL_ROUNDS = 3` 与
   `ReActLoop(max_steps=8)` / `TaskAgent` 默认 8，而 README 架构图写的是"工具循环 <=3 轮"。
   现在两处都读 `settings.agent_max_steps`；`ReActLoop` 的默认值改为 `None` → 解析自设置，
   不留第三个字面值。**这是一处行为变化**：TaskAgent 的 ReAct 上限 8 → 3。

4. **`AGENT_LLM` / `AGENT_MAX_STEPS` 收编进 `app/config.py`**，并写进 `.env.template`。
   此前 `task_agent.py` **直读 `os.environ`**、绕过配置层，且 `.env.template` 完全没记录
   这两个开关——"有真拆解"（`AGENT_LLM=litellm`）这件事没人知道怎么打开。收编后自动进入
   ADR-012 那条配置单一来源守卫（`test_config_consistency.py` 的 `SINGLE_SOURCE_CONCEPTS`
   新增该概念），再有人从别处读 env 就会红（守卫同样自测过会红）。

5. **活体评测路径的未 await 协程警告：定位到上游，登记而不硬修**。真跑
   `evals/run_evals.py`（非 `--offline`）时 stdout 抛
   `RuntimeWarning: coroutine 'OpenAIChatCompletion.acompletion' was never awaited`，
   而测试套件 0 warning——正是"绿信号掩盖真问题"。定位结果：**与我们的代码无关**，
   3 行复现（仅 `litellm.acompletion` + `asyncio.wait_for`，无本项目代码）即可复现；
   触发点是 `app/router/intent_router.py:93` 用 `asyncio.wait_for` 从**外部取消**
   litellm 调用（litellm 1.96.2 在取消时留下未 await 的内部协程）。
   已验证的替代方案：改用 litellm **自身的 `timeout`**（把超时传进去而不是从外部取消），
   实测干净抛出 `litellm.Timeout` 且无该警告。**本轮不做**——那要给共享的 `LLMClient`
   加超时参数并改变路由超时语义，而路由超时正是 ADR-011 记录过"意图摆动"真因的敏感区，
   需要独立一轮验证。已登记在 README 已知边界。

## 后果

- 测试 **780 → 789 passed / 0 skipped**（新增 8 条审计字段守卫 + 1 条配置守卫参数化用例）；
  `ruff`、`bandit -r app` 干净；红队门禁 exit 0；`eval --offline` 退出码 0。
- **一处行为变化**：TaskAgent 的 ReAct 步数上限 8 → 3（全量测试无回归；
  `test_react_loop` / `test_engine_parity_golden` 等显式传参的用例不受影响）。
  取更严的值是有意的：README 的架构图与"有界 Agent"叙事本来就说的是 3。
- **一处已知上游问题**：litellm 在外部取消时留下未 await 协程（上面第 5 条），
  当前表现为一条 `RuntimeWarning` + 一个可能未被清理的内部请求；替代方案已验证但未实施。
- **仍未做、且明确不做**（沿用 ADR-014 与减法清单）：语义级结果验证、LLM 摘要压缩、
  回调 RBAC 角色、PR 审查写回 GitHub、OTel 真接线、评测基线对比。
- **可复用的教训**：与 ADR-014 的"测试先红、实现才改对"同源，本轮补两条——
  **"文档声称可查"必须能被一条测试证伪**（否则字段丢了没人知道，README 还替你担保）；
  **"测试 0 warning"不等于"没有警告"**，活体路径（评测、真实回调）要单独跑一次才算数。
