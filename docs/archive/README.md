# 归档文档

2026-10-19 收口时移进来的历史文档。它们记录了项目在某几个时期的**打算**，不是**现状**：
其中的路径、基线测试数、模块状态都已过期。当前口径只看 `README.md`、`delivery/定位与减法清单.md`
与 `docs/adr/`。

规则：**归档区的文件不再维护**。发现某条主张与代码不符，不要在这里改——要么在 ADR 里记账，
要么直接删掉这一条对外宣称。

## 内容

| 文件 | 来源 | 为什么归档 |
| :--- | :--- | :--- |
| `RESUME_V2_PLAN.md` | `docs/` | "把 moa-gateway 改造成 agent-gateway"的计划，基线 362 tests（2026-08），未被采纳 |
| `HANDOFF.md` | `docs/` | 上一条的执行版，自承基线已过期 |
| `stages.md` | `docs/` | "MoA Engine 完整阶段计划"，基线 16 tests（2026-08-05） |
| `NO_SUBSTITUTES.md` | 仓库根 | Stage 1 交接清单，基线 9 tests |
| `高层架构设计.md` | `delivery/` | AICoding 文档流水线产物（2026-08），未按此实施 |
| `系统设计.md` | `delivery/` | 同上 |
| `UserStory.md` | `delivery/` | 同上 |
| `集成交付说明.md` | `delivery/` | 同上 |
| `research_report.md` | `delivery/` | 同上；其中的 `✅ 已引入 RAGAS / DeepEval / Promptfoo / Prometheus / Grafana` 全是假的 |

**留在原地的活文档**（就地更新，不要搬进来）：`docs/multi-agent-collaboration.md`、
`docs/retrieval-evaluation.md`、`docs/redteam.md`、`delivery/material_digest.md`、
`delivery/定位与减法清单.md`、`delivery/可靠执行底座-重构计划.md`。

另有两份带日期的历史记录留在原位、同样不维护：`docs/experience-fix-2026-08-16.md`
（当时的修复记录）、`docs/market-research-2026-09-14.md`（当时的选方向调研，其
"推荐中文合同审查方向"的结论已被当前定位取代）。
