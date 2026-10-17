# 08 — 聚合与剂量曲线报告

**What to build:** 一次 run 一份 JSON 是原料，不是结论。把 `RunResult` 聚合成 §5.4 那张表：每一格是通过率与成本，每一列减去前一列就是"每加一个 agent 的边际收益"。报告要能从落盘 JSON 离线重算（可复现），并且在没打真实 API 的情况下也能用 fixtures 跑通。

**Blocked by:** 02 — arm 缝

**Status:** done

- [x] 聚合器读一批 `RunResult` JSON，产出按 任务 × 档位 的表
- [x] 每格含通过率与成本；相邻档位的边际收益可算
- [x] `budget_exhausted` / `error` 单独成列，不与 `fail` 混算
- [x] 离线 fixtures 驱动；同一批输入重算结果确定
- [x] 报告标注样本数与缺口，不把 n=1 说成曲线
- [x] 不接真实 API、不依赖网络

**落地：** `bench/report/aggregate.py` 是聚合器，`bench/report_curve.py` 是离线 CLI
（`--runs <文件或目录> --tier B1|B3 [--out report.json]`）。输入是 `bench/run_task.py`
落的 `RunResult` JSON，`RunRow.from_dict` 只取聚合要用的字段（task / arm / outcome /
成本 / token / 档位 / 模型 / wall），因此对 `RunResult` 增字段免疫。

产出 `DoseReport`：`cells` 是 任务 × 档位 的一格（通过率、平均成本、四类结局计数），
`columns` 是每档跨全部任务的合计，`marginals` 是 `后一档 − 前一档` 的 Δ通过率与
Δ平均成本（任一侧没样本记 `None`，不是 0）。四档（L0–L3）即使一个样本都没有也占一
列——缺了就是缺口，`gaps` 把它们列出来。

三种"没通过"分开：`pass` / `fail` / `budget_exhausted` / `error` 各自计数，通过率只看
`pass`；`render_markdown` 除了主表，另出一张结局分布表把它们**各自成列**，避免
"协作太贵"被误读成"协作不行"（§8）。报告顶部标注样本数与缺口，`is_single_sample`
（每格 ≤ 1 次运行）为真时显式写"这是单点样本，是猜测不是曲线"（§9）。

确定性：`load_rows_dir` 按文件名排序，聚合只做纯算术与排序，`to_dict()` 只吐数字且
可 JSON 序列化。同一批输入两次聚合，`to_dict()` 与 `render_markdown()` 逐字相同。

离线用例：`tests/unit/test_bench_report.py`（表与缺口、结局分列、边际收益、B1/B3 过滤、
重算确定性、落盘 JSON 读回、单样本提示、`from_dict` 容错）。

## code-review 补记（全 branch 复评）

复评在两轴各挑出几处，逐条改：

* **通过率不再把 `error` 算进分母。** §8 要求 `error`"单独统计、不混进通过率"；原来
  `pass/runs` 让一条 API 噪音压低通过率。现在 `valid_runs = runs - error`，通过率是
  `pass/valid_runs`（无有效样本记 `None`，不是 0）；`budget_exhausted` 留在分母里
  （在预算内没通过就是没通过），但它仍单独成列。用例把 1 pass + 1 error 的期望钉成
  100%（原来错写成 25%）。
* **每个数字能追回具体 run。** `RunRow.source` 带来源文件名，`DoseReport.sources`
  随 `to_dict()` 落盘并在报告顶部列出——原报告的"可追回"是空话。
* **缺整个任务也要看得见。** `build_report(..., tasks=[...])` 可显式声明应跑的任务集：
  声明了却一条都没跑的任务作为缺口出现，不再从表里悄悄消失。
* **样本不足逐格标。** 原来只在"每格都 ≤1"时警告；现在 `under_sampled` 列出所有
  `< MIN_RUNS_PER_CELL`(=3, §9) 的非空格子并写进报告。
* **B3 增量成本不再被丢掉。** `RunRow` 读 `baseline_cost_usd`，`ArmColumn` 给
  `total_incremental_cost_usd`，B3 报告多一节"相对 B1 的增量成本"。
* **去重与单一来源。** 三处重复的四计数器收进 `OutcomeCounts`（唯一的计数口径）；
  `ARM_ORDER` 改从 arm 注册表 `arm_names()` 取（§4.2 的单一来源），报告层不再自持
  一份可用清单；`budget_tier` 用 `BudgetTier` 字面量类型，`RunRow.from_dict` 对未知
  档位直接抛错而不是静默收下。
* **CLI 补测试。** 复评指出 `bench/report_curve.py` 无 `tests/unit` 覆盖（违反
  NO_SUBSTITUTES 的"新增代码都要有测试"）：新增 `tests/unit/test_bench_report_curve.py`
  覆盖参数契约、目录/文件读取顺序与 `main` 的打印+落盘。
