# 06 — L3：Planner + N 并发 worker + Critic

**What to build:** 把 worker 扇出成并发，并处理并发写收敛——多个 worker 在同一 superstep 写同一个通道会直接 `InvalidUpdateError`（本项目第一版协作图就是这么崩的）。正确形态是：并发 worker 只写带 reducer 的通道，"是否升级、升成什么"由下游**单个**节点裁决。收工时 L3 能并发跑 N 个 worker 而不崩，且失败能定向重跑某个子任务而不是全量重来。

**Blocked by:** 05 — L2

**Status:** ready-for-agent

- [ ] 并发 worker 只写 reducer 通道；升级裁决由下游单点做
- [ ] 离线用例：N=3 并发写不抛 `InvalidUpdateError`
- [ ] 子任务级重跑：一个子任务失败时只重跑它
- [ ] 并发下账本不串：每个 worker 的用量可归因
- [ ] 预算烧光时如实报 `budget_exhausted`，不伪装成完成
