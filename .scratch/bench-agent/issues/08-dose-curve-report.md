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
