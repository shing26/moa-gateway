# 01 — 任务集：把其余 E1 候选过一遍 fail-first

**What to build:** 剂量曲线要按难度分档，一个任务撑不起一张表。把 §12 里另外四个 E1 候选（GitHub 调用绕过代理、飞书富文本解析、审计链每写者一条、tool_calls 尝试数口径）各自拆成 solution / test 两个 patch 并落成任务定义，然后逐个跑 fail-first：本来就绿、或者修不成一次收敛的候选，连同剔除原因一起如实记下。收工时任务集至少覆盖易 / 中 / 难三档，且每个留下来的任务都有 fail-first 通过的证据。

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] 四个候选各自产出任务定义与两个 patch（solution / test 分开）
- [ ] 每个任务跑 fail-first：通过 fail-first 的留下，不通过的连同原因一起记录
- [ ] 留下来的任务覆盖至少三档难度
- [ ] 逐任务跑通端到端：scripted agent 写真修复 → `pass`；只改测试 → `fail`
- [ ] 既有 988fccb 任务不被改动
