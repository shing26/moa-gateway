# 03 — L1：单 agent + Critic 有界回边

**What to build:** 第一次引入回边。agent 自报完成后，Critic 看它的 patch 与验证输出，给出**可执行的**修正指令（不是"再检查一下"这种没有落点的反馈），循环按有界轮数再来一次。收工时 L1 能端到端产出 `AgentRun`，并且"Critic 打回 → 再修 → 通过"与"一直打回 → 有界停下"两条路径各有一条离线用例。

**Blocked by:** 02 — arm 缝

**Status:** done

- [x] L1 走同一个 `CodingAgent` 接口，与 L0 的唯一差别是 Critic 回边
- [x] Critic 反馈转成可执行的下一步指令：断言它进入了下一轮上下文，而不是只写进日志
- [x] revise 轮数有硬上限；超出后如实报 `step_limit` 或 `budget_exhausted`
- [x] 离线用例：打回一次后修好 → `pass`；一直打回 → 有界停下且不伪装成功
- [x] 预算与步数账本把 Critic 的调用一起算进去

**落地：** 回边复用**同一条**会话。`bench/agent/loop.py` 把"继续"这条缝拆成
`LoopSession`（消息历史 + 轨迹 + 账本 + `advance()`），`LLMBackedAgent.run()` 只是
它的一次推进，L0 的轨迹与账本不变（`test_bench_agent_loop.py` 是那道门）。
`bench/arms/l1.py` 的 worker 是**中性的单 agent**（`LLMBackedAgent`，不给专家角色，
这样"唯一差别是回边"字面成立），它持有同一个会话反复推进，Critic 的指令作为一条
user 消息追加进去——这正是"别重复撞同一面墙"。Critic 直接用 §5.1 专家池里的
`review` `ToolExpert`（能自己重读、重跑），不另造第三套循环；它的终局经
`bench/agent/critic.py` 的保守解析器落成 `approve` / `revise(指令)`，读不出来一律
判打回。账本是同一份：worker 跨回边累计，Critic 每轮的 `Usage` 并进来；步数也来自
同一条 `Budget.max_steps`（给 Critic 留步数是"从剩余额度里分"，不是另给一份）。
回边硬上限是 `ReviewPolicy.max_rounds = 2`，到顶报 `step_limit`
（`AgentRun.review_rounds` 记实际轮数）。Critic 不给判决——harness 仍可能把自报
`completed` 判成 `fail`。
