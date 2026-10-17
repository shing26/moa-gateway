# 03 — L1：单 agent + Critic 有界回边

**What to build:** 第一次引入回边。agent 自报完成后，Critic 看它的 patch 与验证输出，给出**可执行的**修正指令（不是"再检查一下"这种没有落点的反馈），循环按有界轮数再来一次。收工时 L1 能端到端产出 `AgentRun`，并且"Critic 打回 → 再修 → 通过"与"一直打回 → 有界停下"两条路径各有一条离线用例。

**Blocked by:** 02 — arm 缝

**Status:** ready-for-agent

- [ ] L1 走同一个 `CodingAgent` 接口，与 L0 的唯一差别是 Critic 回边
- [ ] Critic 反馈转成可执行的下一步指令：断言它进入了下一轮上下文，而不是只写进日志
- [ ] revise 轮数有硬上限；超出后如实报 `step_limit` 或 `budget_exhausted`
- [ ] 离线用例：打回一次后修好 → `pass`；一直打回 → 有界停下且不伪装成功
- [ ] 预算与步数账本把 Critic 的调用一起算进去
