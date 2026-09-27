# ADR-0016: 收尾台账——剩余登记项的触发线与下一轮方案

日期：2026-09-24
状态：已实施（登记类；两条小尾巴随本 ADR 一并清掉）
关联：ADR-014（收尾三态）、ADR-015（诚实收敛）、ADR-011（路由超时与意图一致性）

## 背景

第十、十一轮把"声明与实现不符"清零之后，剩余事项收敛为三类：
**登记未修**（litellm 未 await 协程）、**文档/环境小尾巴**、**明确不做与缺外部条件**。

按 ADR-014 的原则（每项落到"实现 / 删除 / 明确不做"三态、不留待办），光说"不做"还不够——
每一项都得写明**重新打开的条件（触发线）**。否则它们会在未来的盘点里被反复当成"新缺口"
（2026-09-24 的六维复评就发生过一次：README 已声明为有意边界的事项又被列进缺口清单）。

## 决策

1. **两条小尾巴本轮清掉**：

   - README 上下文预算条目的裁剪键名改为**实际落盘键**。此前写的是
     `kept/dropped/elided/tokens`，而 `context_budget` 实际落盘的是
     `enabled / history_in / history_kept / history_dropped / elided / summary_tokens /
     summary_truncated / budget` 八个键——措辞与事实对齐，别让读者照着错的键名去查。
   - CI 升级 `actions/checkout@v4→v5`、`actions/setup-python@v5→v6`（Node 24 原生运行时，
     消除"Node 20 弃用、被强制跑在 Node 24"的告警）；同时把两个 job 的
     `runs-on: ubuntu-latest` 钉为 `ubuntu-24.04`——GitHub 公告 ubuntu-latest 自
     2026-10-19 起迁移到 Ubuntu 26，钉住等于"迁移日零意外"（沿用今天的环境），
     想跟进 Ubuntu 26 时改那一行即可。

2. **litellm 未 await 协程：独立一轮（第十二轮），方案与验收先写死**。
   现状：真跑 `evals/run_evals.py`（非 offline）时 stdout 抛
   `RuntimeWarning: coroutine 'OpenAIChatCompletion.acompletion' was never awaited`；
   已定位为 litellm 1.96.2 被**外部取消**时留下未 await 的内部协程（3 行复现、无本项目代码），
   触发点是 `app/router/intent_router.py:79,93` 两处 `asyncio.wait_for(classify(...), timeout)`。

   - **首选方案**：先试升级 litellm——若上游已修，一行依赖变更即可收工。
   - **自修方案**：把超时从"外部取消"改为"传进去"。给 `LLMClient.chat / chat_with_tools /
     _acompletion / _build_kwargs` 增加 timeout 参数（默认仍取 `LLMConfig.timeout`），
     `LLMIntentClassifier.classify` 透传，`IntentRouter` 把 `router_timeout_ms` /
     `micro_timeout_ms` 传下去后**移除两处 `asyncio.wait_for`**。
     已验证该路径可行：对 litellm 传自身 `timeout` 会干净抛 `litellm.Timeout`、无泄漏。
   - **验收标准（全部满足才算完成）**：① 真跑 e2e（非 offline）stdout 无该 RuntimeWarning；
     ② `intent_consistency` 热态下 stable/agreement 不低于现基线；③ 路由降级语义不变——
     冷启动仍降级到默认意图且 `route_fallback` 如实记录；④ 全量测试 0 回归。
   - **为什么独立成轮**：这是 ADR-011 的敏感区（"意图摆动"的真因就在路由超时），
     改超时语义必须带着它自己的验证走，不能搭车。

3. **明确不做 + 缺外部条件：逐项登记触发线**。到线之前它们不是待办；盘点时以本表为准，
   表外新发现才立项。

   | 项 | 触发线（什么条件出现才重新立项） |
   |---|---|
   | 回调 RBAC 角色校验 | 多角色共用同一套审批（当前白名单只答"能不能批"已够用） |
   | PR 审查写回 GitHub | 有真实仓库的消费者使用该链路（现为只读审查） |
   | OTel 真接线（OTLP exporter / collector） | 多实例部署或 >5 人日常使用 |
   | 评测基线对比（本次 vs 上次 delta） | 出现会消费评测结论的决策点——否则又是一个没人读的 JSON |
   | 语义级结果验证、LLM 摘要压缩 | "治理输出"定位变化或真实用户提出（与减法清单冲突） |
   | 成本量化（`avg_cost_usd` 恒 0） | 接入付费 provider |
   | `hitl_feedback` join 率与真实介入率 | 有真实流量（当前 4 条全为模拟种子） |
   | 微模型那一级路由 | 有第二个模型端点 |
   | 路由冷启动不降级 | 模型常驻，或演示前预热（操作说明已在 README） |
   | 多实例化（会话状态/预算累计器/限流器外部化） | 真要跑多实例（`redis_state/store.py` 是活的，按当时需求设计） |

## 后果

- 剩余事项**全部落账**：无未登记的开放项；后续盘点以本表为准，表外新发现才立项。
- CI 不再有第三方动作告警，runner 版本显式化。
- litellm 警告在第十二轮之前仍会出现在真跑 e2e 的 stdout 里（README 已知边界已注明）。
