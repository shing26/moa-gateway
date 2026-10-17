# 07 — 预算策略：等预算（B1）与放宽复跑（B3）

**What to build:** 主线实验问的是"同等花费下谁更强"，所以四档必须给同一个美元上限；B3 再把主线里失败的档位放宽预算跑一遍，回答"协作能不能做到单体做不到的事"，并诚实标注它多花了多少。收工时 runner 支持这两档预算，落盘里能分辨某次 run 属于哪一档。

**Blocked by:** 02 — arm 缝

**Status:** done

- [x] B1：四档共用同一个美元上限
- [x] B3：只对 B1 里失败的档位放宽上限复跑，并记录相对 B1 的增量成本
- [x] `RunResult` 能区分这次 run 属于 B1 还是 B3
- [x] token 上限作为兜底保留，但不做主线判据
- [x] 离线用例：同一 arm 在两种预算档下的产物可分辨

**落地：** 两档预算的单一来源是 `bench/harness/budget.py` 的 `BudgetPolicy`：
`b1()` 给四档**同一个** `Budget`（美元上限一致，档位之间唯一变量是编排拓扑），
`b3()` 默认取主实验的三倍（放宽到足以区分"做不到"与"预算不够"；具体数值仍按
§12 由真实 run 反推）。`for_tier("B1"|"B3")` 是 CLI 与测试共用的入口，未知档位报错。

哪些档位进 B3 复跑由 `b3_candidates(results)` 判定：只取 `budget_tier == "B1"` 且
`outcome` 为 `fail`（没做成）或 `budget_exhausted`（太贵）的 `(task, arm)`——
`pass` 不必复跑，`error` 是 API/超时噪音、放宽预算也答不了能力问题，排除掉才不污染
B3。去重保序。

`RunResult` 新增两个字段把两档写进落盘物：`budget_tier`（`B1`/`B3`）与
`baseline_cost_usd`（同 `(task, arm)` 的 B1 花费），并有派生属性
`incremental_cost_usd = max(0, 本次 - 基线)`——§5.3 要求诚实标注"多花了多少"。
`run_task` 透传这两个参数；CLI 加了 `--budget-tier` / `--baseline-cost-usd`，
`--max-cost-usd` / `--max-steps` 从固定默认值改成"覆盖档位默认值"（不传就用策略）。

token 上限是第二条兜底（§5.3），落成 `Budget.max_tokens`（`0` = 不设）、
`Usage.total_tokens`、以及 `Budget.stop_status` 与 `LoopSession.advance` 里与美元
并列的一道闸门。命中它同样归 `budget_exhausted`（"想继续但没额度了"是同一件事），
主线判据仍是美元。全局预算切段（`remaining`）时它原样下传，L3 的 lane 不按并发数
均分它——它是防单条 lane 刷 token 的全局闸门，与按美元均分是两件事。

离线用例：`tests/unit/test_bench_budget.py`（B1 单一预算、B3 更宽且共用 token 兜底、
未知档位报错、B3 候选筛选与去重、两档产物可分辨、token 兜底判停）；`test_bench_agent_loop.py`
补一条 token 兜底命中即 `budget_exhausted` 且不执行工具；`test_bench_harness.py` 的
端到端补一条 B3 档位与增量的透传断言。

## code-review 补记（全 branch 复评）

* **B3 从"有筛选函数"补成"有执行口径"。** 复评指出：`b3_candidates` 只回答"该复跑
  谁"，没有任何路径**保证**只对 B1 失败档位复跑，基线花费还得调用方手填（默认 0）——
  增量成本因此不可靠。新增 `plan_b3_reruns(results, policy=...)`：由 B1 结果直接推出
  `(task, arm, baseline_cost_usd, budget)`，基线是该档 B1 全部尝试的花费之和，复跑用
  `policy.b3()`。增量成本现在是推出来的，不是填出来的。补用例
  `test_plan_b3_reruns_derives_the_baseline_from_b1_results`。
* **token 兜底从"机制"补到"可及"。** 复评指出 `max_tokens` 默认 0 且 CLI 没有开关，
  等于死代码。CLI 新增 `--max-tokens`，可覆盖档位默认值。至于"该不该是全局上限"——
  §5.3 的原话是"防止**单个 agent** 无节制地刷 token"，所以按 (会话/lane) 各自计是对的
  粒度，维持不改；记录在此备查。
* `BudgetTier` 字面量类型上移到 `bench/agent/types.py`（`RunResult` 要带它落盘，放
  `harness/budget.py` 会绕成 import 环），`budget.py` 转成 re-export——档位字面量因此
  只有一处定义。
