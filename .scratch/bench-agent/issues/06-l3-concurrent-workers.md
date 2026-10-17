# 06 — L3：Planner + N 并发 worker + Critic

**What to build:** 把 worker 扇出成并发，并处理并发写收敛——多个 worker 在同一 superstep 写同一个通道会直接 `InvalidUpdateError`（本项目第一版协作图就是这么崩的）。正确形态是：并发 worker 只写带 reducer 的通道，"是否升级、升成什么"由下游**单个**节点裁决。收工时 L3 能并发跑 N 个 worker 而不崩，且失败能定向重跑某个子任务而不是全量重来。

**Blocked by:** 05 — L2

**Status:** done

- [x] 并发 worker 只写 reducer 通道；升级裁决由下游单点做
- [x] 离线用例：N=3 并发写不抛 `InvalidUpdateError`
- [x] 子任务级重跑：一个子任务失败时只重跑它
- [x] 并发下账本不串：每个 worker 的用量可归因
- [x] 预算烧光时如实报 `budget_exhausted`，不伪装成完成

**落地：** `bench/arms/l3.py` 的拓扑是 **Planner → 一个 wave 内 N 路并发 worker →
单点 Critic**，planner 与 Critic 原样复用 L2（`L3ConcurrentWorkers` 继承
`L2PlannerWorkerCritic`，只覆盖"怎么跑子任务"与"重跑谁"两处）。并发用
`asyncio.gather` 扇出；worker **只返回自己的结果**，不写任何共享裁决状态，wave
结束后由 `bench/agent/execution.py` 的 `merge_execution` reducer 按下标合并。
升级裁决不在并发里做——Critic 是唯一的汇聚点（`test_concurrent_over_budget_stops_
before_the_critic` 钉住：wave 越预算时 Critic 压根不跑）。

记账不串：每条 lane 用一个 `clone_llm_factory` 深拷贝出来的**独立 client**——
`last_metrics` 是 client 上的单值通道，共用一条会被后写覆盖。每个 worker 的用量落成
`SubtaskUsage(index, usage, turns, status, model)`，`AgentRun.subtask_usage` 因此是
结构上可归因的，而不是靠调用点约定记谁是谁。重跑时 reducer 对**产出**同下标覆盖、
对**账本**同下标累加，切片之和与总量对得上（`test_merge_execution_overwrites_the_
outcome_and_accumulates_the_ledger`）。

定向重跑：Critic 打回时用 JSON 里点名的 `targets`（`bench/agent/parsing.py` 的
`nonnegative_indices` 清洗，与 planner 的 `depends_on` 同一处）只重跑那些下标，
未点名的子任务结果与账本原样保留；`targets` 为空或全是未知下标时退回整计划
（`test_targeted_revision_reruns_only_the_named_subtask`）。

预算：每个 wave 先取全局剩余额度，再按本轮 worker 数**均分**给各 lane——并发调用
因此不会各自拿到一份满额预算；某个 lane 花得少，剩余留给后续 wave 与 Critic。
账本以真实累计为准，四档共用同一个美元上限（§5.3）。

离线用例：`tests/unit/test_bench_arm_l3.py`（注册、barrier 证 3 worker 同 wave、
独立归因、定向重跑、并发越预算停在 Critic 前、worker factory 失败转 error、
CancelledError 原样上抛）、`tests/unit/test_bench_execution.py`（reducer）、
`tests/unit/test_bench_critic.py`（JSON `targets`）、`tests/unit/test_bench_planner.py`
（review prompt 含 `targets`）。

## code-review 补记

两轴评审（Standards / Spec）发现的处置：

* **ADR-010：取消被吞。** `asyncio.gather(..., return_exceptions=True)` 会把子任务的
  `CancelledError` 当成返回值，原实现一律 `str(exc)` 收成 `status="error"`——取消、
  `KeyboardInterrupt`、`SystemExit` 都被伪装成"某个 worker 出错"。现在只把
  `Exception` 收成 `_failed_worker`，其余 `BaseException` **原样上抛**
  （ADR-010 决策 4）。补 `test_worker_cancellation_propagates_instead_of_becoming_error`。
* **未测的 `with_status`。** `bench/agent/execution.py` 里这个 helper 无任何引用
  （NO_SUBSTITUTES.md 要求新增代码都有测试）。属投机泛化，已删除。
* **重复的 deepcopy 工厂。** `lambda index: copy.deepcopy(llm)` 原先在 `__init__`
  默认值与 `build_l3` 各写一遍。收敛成 `clone_llm_factory`，拷贝策略只有一处。
* **重跑丢账。** reducer 原先对 `subtask_usage` 也同下标覆盖，被重跑的子任务只留
  最后一轮用量，前几轮只剩总量、归因不到 worker。现在账本同下标累加，切片之和与
  总量一致（§6）。
* **`only` 命名不清。** `_run_subtasks` 的参数改成 `rerun_indices`，明说它是"要重跑的
  子任务下标"。

未改、作为判断保留的（连同理由）：

* **reducer 是 bench 内的 Python 合并，不是 LangGraph 通道。** ticket 的
  `InvalidUpdateError` 是 LangGraph `collaboration.py` 的产物；ADR-023 决策 1 已定
  原语落 `bench/`、`app/` 零改动，所以这里用 `asyncio.gather` + reducer 复刻同一
  语义（并发写收敛、单点裁决）。离线用例证明的是"三路并发不崩且收敛"，不是某条
  具体通道不抛异常。
* **worker 基础设施故障不触发重跑。** `error` / `budget_exhausted` / `step_limit`
  是"这条 lane 没做成"，不是"这条子任务的产出需要改"。定向重跑由 Critic 的裁决
  驱动（`targets`），与 §8"预算/错误属自报、是否达标属判决"一致。
* **最后一口越额仍报 `completed`。** Critic 认可发生在成本越顶之后时，按 §5.0 的
  判停顺序"本轮返回终局即 `completed`"（**完成优先于预算**），不算伪装成功——
  agent 确实做完了；`budget_exhausted` 留给"想继续但没钱了"。
* **lane 均分可能在一个 lane 耗尽份额而全局未到顶时报 `budget_exhausted`。** 这是
  均分预算的既定语义（模块 docstring 已写明），报告里的 `usage` 是真实累计，读者
  据此能看到实际花费仍未到顶。
* **`depends_on` 不影响调度。** §5.2 要的是 fan-out 并发、reducer、单点裁决，
  没有拓扑排序的要求；解析它只是不在读计划时把它悄悄丢掉。按依赖分 wave 是后续
  增强，不在 06。
* **L2 的 review 提示词也广告了 `targets`。** prompt 由 L2/L3 共用，L2 的
  `_rerun_targets` 依旧返回 `None`（全量重跑），只是提示里多了一段 L3 才用得到的
  格式说明——记录在案。
