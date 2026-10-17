# 05 — L2：Planner + 1 worker + Critic

**What to build:** 引入任务分解与 handoff，但 worker 只有一个——先把"上下文怎么从规划者传到执行者"这一课单独跑通，再加并发。收工时 L2 能对同一个任务产出 `AgentRun`，且"规划出的子任务带够了上下文、执行者不必回头问"这条有一条离线用例钉住。

**Blocked by:** 03 — L1；04 — 专家接工具循环

**Status:** ready-for-agent

- [ ] L2 走同一个 `CodingAgent` 接口，与 L0 / L1 的差别只在拓扑
- [ ] planner 输出结构化子任务；每个子任务自带足够上下文交给 worker
- [ ] Critic 裁决接在 worker 产出之后
- [ ] 离线用例：一个需要两步才能做完的任务在 L2 上通过
- [ ] 预算账本含 planner 的开销——协调开销是协作成本的一部分，不许漏记
