# 05 — L2：Planner + 1 worker + Critic

**What to build:** 引入任务分解与 handoff，但 worker 只有一个——先把"上下文怎么从规划者传到执行者"这一课单独跑通，再加并发。收工时 L2 能对同一个任务产出 `AgentRun`，且"规划出的子任务带够了上下文、执行者不必回头问"这条有一条离线用例钉住。

**Blocked by:** 03 — L1；04 — 专家接工具循环

**Status:** done

- [x] L2 走同一个 `CodingAgent` 接口，与 L0 / L1 的差别只在拓扑
- [x] planner 输出结构化子任务；每个子任务自带足够上下文交给 worker
- [x] Critic 裁决接在 worker 产出之后
- [x] 离线用例：一个需要两步才能做完的任务在 L2 上通过
- [x] 预算账本含 planner 的开销——协调开销是协作成本的一部分，不许漏记

**落地：** `bench/arms/l2.py` 的拓扑是 **分解 → 逐子任务交接 → 评审**，循环与工具全部
复用 §5.0 的 `LoopSession`；worker 是与 L0 相同的中性单 agent，Critic 是 L1 用的
`review` 专家（同一条 `parse_verdict`；打回后按 L2 的拓扑全量重跑，子任务级定向重跑
留给 06）。

handoff 是这一档真正在测的东西：**每个子任务开一条新会话**，worker 只拿到
`instruction` + planner 附的 `context` + 前序子任务的产出——它翻不到 planner 的脑子，
所以"带够上下文"是可检验的（`test_next_subtask_sees_the_previous_result`）。把两条
子任务塞进同一条会话会让上下文"顺手"带过去，恰好测不出这条缝；分开开会话是刻意的
代价，留给 L3 去处理历史复用。

planner 的输出经 `bench/agent/planner.py` 的 `parse_plan` 解析：只接受 JSON（容忍
围栏与括起来的对象），`instruction` 与 `context` 都是必填；缺任一字段都抛
`PlanParseError` 并由 L2 如实记成 `error`——**不编一份看起来像的计划，也不接受
一个无法交接的半成品子任务**。账本是同一份：planner、每个 worker、每轮 Critic 的
`Usage` 并进同一个出口，步数与美元都共用同一条全局预算（每段从剩余额度里分，
只允许真正发出调用的那一轮越额）。
`AgentRun.subtask_count` 记分解规模，`review_rounds` 记回边轮数。

离线用例：合成一个"两个常量都在才过"的两步任务，`run_task` 端到端跑出 `pass`
（`test_two_step_task_passes_end_to_end_through_the_harness`）；另有 handoff 明文、
全量打回重跑、planner 不可读计划、缺失上下文拒绝、全局预算、协调开销入账各一条。

## code-review 补记

两轴评审发现并已修：

* **全局预算没有真正执行。** planner / worker / Critic 原先把同一条 `Budget` 当成
  各自可用的满额预算，阶段越多总花费越可能成倍越顶；`max_steps=0` 也仍会给 planner
  一次调用。现在每段只拿 `Budget.remaining()` 的剩余额度，阶段边界先做全局
  `stop_status()`；L1 续跑同一 worker 时用 `cost_limit_usd` 限制本段新增成本，并把
  Critic 的轮数、worker 第二轮的用量都并回同一账本。
* **handoff 只实现了一半。** `Subtask.context` 原先是空串默认值，编号行兜底永远
  产不出上下文却仍被接受。现在只接受带非空 `context` 的 JSON 子任务，缺失即整份计划
  报错。
* **`targets` 不可达且越界。** Critic 提示词从来只要求 `APPROVED` / `REVISE`，
  解析器却能读 JSON `targets`，真实模型因此到不了定向重跑；那是 06 的课题。已删除
  `targets` 与 `_rerun` 的分支，L2 打回后全量重跑。
* **账本与解析去重。** `Usage.__add__` 取代 L1/L2 两份 `_merge_usage`；删除 Critic
  的 `_targets` 后，原先与 planner `_dependencies` 重复的索引清洗只剩一处。
* **命名与覆盖。** `_Exec` 改成 `SubtaskExecution`；补 `ExpertPool.temperature`
  两条分支、全局预算、零步数、缺失上下文与 `Usage`/`Budget` 共享算术的测试。
