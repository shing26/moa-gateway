# 多轮评测预注册（protocol `multiround-v1`）

**冻结于运行之前**（2026-11-17）。本文件登记的是**即将**执行的评测协议；仪器已随
契约交付（`eval/aggregate.py`、`scripts/eval_report.py`、`scripts/run_eval_baseline.py
--rounds`），但**本契约不含任何真实轮次**。轮次开跑需要所有者明确批准（见 §7）。

## 1. 为什么要多轮

`docs/acceptance/eval-baseline-v2.md` §2：同一模型、同一口径、同样 17 条，两次单轮
运行有 **6 条换了 verdict**（4 条 FAIL→PASS，2 条 PASS→FAIL）。单次运行的结果是
一次抽样，不是用例难度或模型能力的读数。任何通过率都必须以轮次分布 + 区间呈现；
单轮数字只能内部参考。

## 2. 冻结参数

| 项 | 值 |
|---|---|
| 用例集 | 26 条（`eval/cases/{business,failure,boundary,security}.jsonl` 全部用例：21 条 live + 5 条 gate-state 探针） |
| 轮数 | **K = 3** |
| 协议版本 | `multiround-v1` |
| 模型 | `nvidia/nemotron-3.5-super-vl-120b-a12b`（`scripts/run_eval_baseline.py` 的 `ENV_OVERRIDES` 冻结快照） |
| 运行器 | `scripts/run_eval_baseline.py --rounds 3` |
| 臂 | `live`（模型真实跑）与 `gate_state`（确定性探针）分开统计，绝不合并 |

用例语料、模型快照、环境覆盖在开跑前冻结；运行期间不得修改。`eval/l0.py` 的判据
同样冻结。

## 3. 执行顺序

固定为：

1. `business`
2. `failure`
3. `boundary`
4. `security`

族内按对应 JSONL 文件的**文件顺序**（`eval/schema.py:load_cases` 的读入顺序）。
每轮（`round_index = 0, 1, 2`）都按这个顺序完整跑一遍。轮次之间不重排、不抽签。

**禁止**：挑选用例、挑选顺序、按中间结果提前停止或跳轮、按结果挑选时段重跑。

## 4. 排除规则

排除项**单独计数，绝不进入任何分子/分母**（`eval/aggregate.py` 的 `excluded`）：

- `infra`：行终态为 `infra_error`（环境/端点/记录缺失等非被测对象的失败）。
  **允许重跑一次**。因为合并语义不覆盖同 `(case_id, round_index)` 的既有行，
  重跑必须写入**独立报告文件**（例如 `--only <case> --report <name>-retry-r<k>.json`）；
  分析时用重跑行替换原 infra 行再聚合，两次都 `infra_error` 才计入 `excluded.infra`。
  重跑不得改变轮次定义、不得借机挑选用例或时段。
- `not_run_by_design`：语料层面明确不设计的路径。当前 26 条全部可测，此桶预期为 0；
  若出现，只计数、不进率。
- `unscored`：行上没有任何判据字段（`gate_state.passed` / `l0.contract_passed` /
  显式 `passed`）。聚合器把它单独计数，**不猜 pass 也不猜 fail**。

判据来源复用既有语义，不另造：

- `arm == "gate_state"`：`eval/gate_state.py` 的 `GateVerdict.passed`；
- `arm == "live"` 的普通用例：`eval/l0.py` 的 `L0Report.contract_passed`
  （`check_run` 对运行记录打分）；
- `arm == "live"` 的 live gate-state 用例：该行的 `gate_state.passed`。

**开跑前/分析前的必要步骤**：运行器写出的普通 live 行不含 l0 判据（打分需要运行记录，
而 `eval_report.py` 按契约不读 `data/`）。因此分析前必须用既有 `eval.l0.check_run(record,
case)` 对每条 live 行打分，把 `l0.contract_passed` 写回该行，再跑聚合器。未写回的行走
`unscored`，不得手工补判。

## 5. 指标定义（主指标冻结）

- **主指标**：`live` 臂的 aggregate pass rate = `passes / rounds`，附 **Wilson 95%
  区间（z = 1.96）**。区间方法冻结；不许换成 Wald / bootstrap / 其它。
- **逐例向量**：每个用例按轮次顺序的 `P,F` 串（如 `F,P,P`）必须出现在产物
  （`per_case`）里；第三方据此可自行重算。
- **分类分解**：`per_class` 只统计 live 臂，类别为 business / failure / boundary /
  security。
- **gate_state 臂**：单独统计（`gate_state_arm`），只说明门禁机制本身是否有效，
  不并入主指标。
- **终态分布**：`terminal_states` 记录所有行的终态，仅作描述。

`sufficient` 规则：观测到的轮数 < 2 时为 `False`，产物附 `insufficient_rounds`
提示。**K = 3 是宣称门槛；少于 3 轮不得对外宣称通过率。**

## 6. 报告与复算

```
.venv/Scripts/python.exe scripts/run_eval_baseline.py --rounds 3 --report evals/reports/<name>.json
.venv/Scripts/python.exe scripts/eval_report.py evals/reports/<name>.json          # 人读摘要
.venv/Scripts/python.exe scripts/eval_report.py evals/reports/<name>.json --json   # 聚合产物
```

报告合取语义：同一 `(case_id, round_index)` **不覆盖**；新轮次追加。报告内记录
`protocol_version`、`rounds`，每行记录 `round_index`。`eval_report.py` 只读报告文件，
不读数据库、`data/` 或模型端点。

## 7. 成本与批准门槛

按 v2 实测（26 条单轮）：**2.23 小时/轮、15,713,129 tokens/轮**。3 轮预估：

- 墙钟：约 **6.7 小时**（3 × 2.23h，含 8 条读循环用例的预算耗尽路径）；
- tokens：约 **47M**（3 × 15.7M）。

开跑前需要所有者**明确批准**这一成本与时间窗口。未获批准不得启动真实轮次。

## 8. 宣称规则

- **少于 3 轮不得对外宣称通过率**（区间可以算，但只能作为内部参考并标注
  `insufficient_rounds`）；
- 任何对外数字必须同时给出轮数、分子/分母、Wilson 95% 区间与逐例向量出处；
- gate_state 臂与 live 臂不得合并成一个「总通过率」。

## 9. 本预注册不包含什么

- **不含真实运行**：本文件与仪器是一次性交付物，开跑是另行批准的独立动作；
- 不修改 `eval/l0.py` 判据、用例语料、模型/环境快照；
- 不宣称任何当前通过率。
