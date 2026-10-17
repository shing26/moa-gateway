# 08 — 聚合与剂量曲线报告

**What to build:** 一次 run 一份 JSON 是原料，不是结论。把 `RunResult` 聚合成 §5.4 那张表：每一格是通过率与成本，每一列减去前一列就是"每加一个 agent 的边际收益"。报告要能从落盘 JSON 离线重算（可复现），并且在没打真实 API 的情况下也能用 fixtures 跑通。

**Blocked by:** 02 — arm 缝

**Status:** ready-for-agent

- [ ] 聚合器读一批 `RunResult` JSON，产出按 任务 × 档位 的表
- [ ] 每格含通过率与成本；相邻档位的边际收益可算
- [ ] `budget_exhausted` / `error` 单独成列，不与 `fail` 混算
- [ ] 离线 fixtures 驱动；同一批输入重算结果确定
- [ ] 报告标注样本数与缺口，不把 n=1 说成曲线
- [ ] 不接真实 API、不依赖网络
