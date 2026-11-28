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

> **修订 A1（2026-11-28）**：「模型」这一行还隐含一个未被冻结的前提——不得有降级链。
> 该前提已显式钉进 `ENV_OVERRIDES`（`LLM_FALLBACK_MODELS=""`），见 §10。

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
- `unscored`：行上没有任何判据字段（`l0.contract_passed` / `gate_state.passed` /
  显式 `passed`），或 `l0 is None` 且无其它判据。聚合器把它单独计数并在摘要里列出
  原因，**不猜 pass 也不猜 fail**。

判据来源复用既有语义，不另造；优先级固定：

1. `gate_state.passed`：用例自己的 oracle。4 条 live gate-state 用例在运行器里同时带
   `l0` 与 `gate_state`，但它们的 `allowed_paths` 为空，`eval/l0.py` 的
   `changed_paths_match` 结构性恒 False；若让 l0 优先，门禁判定正确的行会被算成失败
   （契约修订 1）。`arm == "gate_state"` 的确定性探针同样走这里；
2. `l0.contract_passed`：普通 live 行。运行器在每条 live 行跑完后，用既有
   `eval.l0.check_run(record, case)` 打分，把 `contract_passed`、`all_passed`、六项
   检查的逐项结果与明细写进该行的 `l0`；拿不到运行记录时写 `l0: None` 并附
   `l0_reason`；
3. 显式 `passed`。

**分析阶段的唯一输入是报告文件本身**（每条 live 行含 `l0`）；`.scratch` 里的评分脚本
不再参与任何环节。若某行 `l0 is None`，该行计入 `unscored`，摘要中列出用例与 `l0_reason`；
不得手工补判，也不得把它当失败计入分母。

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

`sufficient` 规则：观测到的轮数 < 3（含 2 轮）时为 `False`，产物附
`insufficient_rounds` 提示。**K = 3 是宣称门槛；少于 3 轮不得对外宣称通过率。**

## 6. 报告与复算

```
.venv/Scripts/python.exe scripts/run_eval_baseline.py --rounds 3 --report evals/reports/<name>.json
.venv/Scripts/python.exe scripts/eval_report.py evals/reports/<name>.json          # 人读摘要
.venv/Scripts/python.exe scripts/eval_report.py evals/reports/<name>.json --json   # 聚合产物
```

报告合取语义：同一 `(case_id, round_index)` **不覆盖**；新轮次追加。报告内记录
`protocol_version`、`rounds`，每行记录 `round_index`，每条 live 行记录 `l0`（判据与
逐项检查明细）。`eval_report.py` 只读报告文件，不读数据库、`data/` 或模型端点；
第三方只凭这一个文件即可复算全部聚合数字。

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

## 10. 修订记录（amendments）

### A1：模型臂必须无降级链（2026-11-28）

**发现的缺陷**：`ENV_OVERRIDES` 冻结了 `CHANGE_AGENT_MODEL`，但 `app/cli.py` 是用
`LLMConfig.from_env("LLM")` 组配置、只覆盖 `model`，所以实际生效的降级链是
`LLM_FALLBACK_MODELS`（`CHANGE_AGENT_FALLBACK_MODELS` 在代码里没有任何读取方）。
主模型 429 时，`app/agents/provider.py:263` 会静默换到 minimax/lightning 继续跑，
而报告里仍写冻结模型名——臂的标签与实际执行不一致。

**证据**（2026-11-28 03:45 起的 K=3 第二批，第一例 `biz-050`）：

- 641.4s、115 次 LLM 调用、1,048,253 prompt tokens，终态 `budget_exhausted`；
- 该例日志 92 次 429；`nvidia/nemotron-3.5-super` 失败 63 次，
  `minimaxai/minimax-m3.1-flash` 被拉起 29 次。

**修订**：`ENV_OVERRIDES` 增加 `LLM_FALLBACK_MODELS=""`，并加回归测试
（父环境给非空值时必须被钉空，且 `LLMConfig.from_env("LLM").fallback_models == []`）。

**适用范围**：下一次 K=3（v1.5）起。此前的两次起跑均已整批 `abandoned`，不产生任何
进入统计的行；本修订不追溯修改任何已有读数。

**副作用（必须如实写进报告）**：没有降级链后，配额压力会直接表现为
`infra_error` 而不是被另一个模型悄悄吸收。§4 的排除规则与「infra 重跑一次」条款
不变，但 `excluded` 计数预计会上升。
