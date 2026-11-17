# Contract — 多轮评测仪器（C 阶段：让「通过率」成为可宣称的数字）

**冻结于实现之前**（2026-11-17）。本契约只交付**仪器与预注册**，不跑真实轮次
（跑轮次需要 Postgres/Redis、模型端点与数小时墙钟；按所有者决定，实跑与 D 阶段一并
等条件就绪）。

## 为什么

`docs/acceptance/eval-baseline-v2.md` §2 给出的最硬的一条事实：同一模型、同一口径、
同样 17 条，两次单轮运行 **6 条换了 verdict**（`biz-077`、`biz-080a`、
`fail-077-4d50e4c0`、`fail-077-08aaa668` 由 FAIL→PASS；`fail-077-07b8da0a`、
`fail-077-5473e7be` 由 PASS→FAIL）。

结论已经写在那份报告里：**单次运行的结论不可用于判断「这个用例难不难」或「这个模型行不行」**。
但仓库目前没有任何东西能产出**分布**：`scripts/run_eval_baseline.py` 每条用例只跑一次，
打分脚本 `score_eval_baseline.py` 留在 `.scratch/`（未入库），没有区间、没有逐例向量。

没有分布就谈通过率，等于拿一次掷硬币的结果宣布硬币的重量。本契约就修这个。

## 设计

### 1. 聚合器 `eval/aggregate.py`（纯函数）

输入：运行器写出的报告 JSON（`rows` 列表，每行带 `case_id` / `class` / `arm` /
`state` / `gate_state` / 可选的 `round_index`）。

输出（数据结构 + `to_dict()`）：

- `per_case`: `{case_id: {"vector": "P,F,P", "passes": 2, "rounds": 3, "arm": "live"}}`
  ——**逐例向量必须出现在产物里**，第三个人据此可以自己重算；
- `live_overall`: `{"passes": n, "rounds": m, "rate": ..., "wilson95": [lo, hi]}`
  ——只统计 `arm == "live"`；
- `per_class`: 同类分解（business / failure / boundary / security）；
- `excluded`: `{"infra": k, "not_run_by_design": j}` ——**单独计数，绝不进分子分母**；
- `terminal_states`: 终态分布（`ready_for_push_approval` / `verify_failed` /
  `budget_exhausted` / `step_limit` / `rejected` / `infra_error` / `wall_timeout` …）；
- `sufficient`: 布尔——`rounds < 2` 时为 `False`，并在 `to_dict()` 里附一句
  `"insufficient_rounds"`，防止读者把单轮数字当结论。

**区间方法冻结为 Wilson 95%（z = 1.96）**。选它的理由：p̂ 接近 0 或 1 时不塌成
零宽区间，而这正是当前数据的样子（10/21、5/5 探针）。

### 2. 报告入口 `scripts/eval_report.py`（可复现）

`python scripts/eval_report.py evals/reports/<file>.json [--json]`：
读报告 → 打印人类可读摘要 → `--json` 时输出聚合结果。**不读数据库、不读 `data/`、
不调模型**——第三个人拿到报告文件就能复算。

### 3. 运行器支持多轮 `scripts/run_eval_baseline.py`

- 新增 `--rounds K`（默认 `1`，默认行为与今天一致）；
- 每行加 `round_index`；
- **累积语义**：同一 `(case_id, round_index)` 已存在时不覆盖；新的轮次追加；
- 报告里记录 `protocol_version`（本轮为 `"multiround-v1"`）与 `rounds`；
- 已有的臂划分（live / gate_state）、环境覆盖、非 TTY 拒绝路径**一律不动**。

### 4. 预注册 `docs/acceptance/eval-multiround-preregistration.md`

冻结：用例 26 条、K = **3** 轮、执行顺序（business → failure → boundary → security，
族内按文件顺序）、infra 规则（重跑一次，二次仍 infra 计入 `excluded.infra`）、
禁止挑选用例/顺序/时段、指标定义（主指标 = live 臂 aggregate rate + Wilson 95%）、
成本预估（按 v2 实测 2.23h/轮、15.7M tok/轮 → 3 轮约 6.7h、47M tok，**需所有者
明确批准后才开跑**）、以及「**少于 3 轮不得对外宣称通过率**」这一条。

## Allowed paths

```
eval/aggregate.py                                   (新文件：纯聚合 + Wilson)
scripts/eval_report.py                              (新文件：报告 CLI)
scripts/run_eval_baseline.py                        (只加 --rounds / round_index / protocol_version)
tests/unit/test_eval_aggregate.py                   (新文件)
docs/acceptance/eval-multiround-preregistration.md  (新文件：预注册)
docs/roadmap-generality-20261117.md                 (只更新 C / D 两行的状态)
plans/multiround-eval-20261117.md                   (本文件)
```

## Acceptance criteria

1. **聚合器是纯函数**：不读环境变量、不读文件、不发网络、不读时钟。同样的输入任意次调用
   结果相同。
2. **Wilson 区间有手算对照**：至少 3 组已知值写进测试，并在注释里写出算式
   （建议：`10/21`、`5/5`、`0/17`），断言到小数点后 4 位。
3. **逐例向量在产物里**：`to_dict()` 必须包含每个用例的向量字符串与臂归属；
   用 v2 报告的形状构造 fixture，断言 `biz-077` 的向量能被还原成 `F,P` 这样的形式。
4. **排除项不进分子分母**：构造含 `infra` 与 `not_run_by_design` 行的 fixture，
   断言它们只出现在 `excluded`，且 `live_overall.rounds` 不含它们。
5. **单轮时 `sufficient == False`**：单轮 fixture 断言这一点，且 `to_dict()` 带
   `insufficient_rounds`。
6. **运行器累积不覆盖**：用假的 case 列表 + 打桩 `run_case`（或直接测报告合并函数），
   断言同 `(case_id, round_index)` 第二次写不覆盖第一次；`--rounds 1` 时报告形状与今天
   兼容（只多 `round_index: 0` 与 `protocol_version` 两个字段）。
7. **CLI 可复算**：`scripts/eval_report.py` 对 fixture 报告文件跑一次，输出与直接调用
   聚合器一致（测试里子进程跑一次）。
8. **预注册先于运行**：文档里写明 K=3、顺序、infra 规则、成本与「少于 3 轮不得宣称」，
   并明确「本契约不含真实运行」。
9. **既有测试全绿**，`tests/` 只新增；`eval/l0.py`、用例语料、模型快照零改动。
10. **证据绑定**：HEAD SHA + `git status --short`。

## Verification commands

```
V1  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit/test_eval_aggregate.py -q
V2  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit/test_l0_scoring.py tests/unit/test_gate_state.py -q   # 文件名以实现方核对为准
V3  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m ruff check .
V4  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit -q
```

`V2` 的文件名以实现方核对为准，PR 里写清；找不到对应测试就只跑 `V1/V3/V4` 并在 PR 说明。

## Forbidden

- 跑真实轮次（本契约只交付仪器；跑轮次要所有者批准）。
- 改 `eval/l0.py` 的判据、改用例语料、改模型/环境快照。
- 用非 Wilson 的区间方法，或把 `infra` / `not_run_by_design` 混进分子分母。
- 覆盖既有报告行（累积语义是硬要求）。
- 把聚合逻辑留在 `.scratch/`（上一版就是这么丢的）。
- 在文档里宣称任何通过率。

---

## 修订 1（2026-11-17，verifier 第一轮 FAIL 之后、合并之前）

verifier 用真实代码路径复现出三处问题，其中第一处会**把正确的门禁判定算成失败**。据此修订，
其余验收标准方向不变。

### 1. 判据优先级：`gate_state` 先于 `l0`（**推翻原指令**）

原指令要求 `l0.contract_passed` → `gate_state.passed` → `passed`。实测后果：

- `gate_state.is_live_gate_case` 的 4 条 live 用例在 `run_eval_baseline.py` 里**同时**拿到
  `l0` 与 `gate_state`；
- 这 4 条的 `allowed_paths` 是**空元组**，而 `eval/l0.py` 的 `changed_paths_match`
  要求 bundle 非空且文件 ∈ allowed_paths，于是**只要运行记录可读，`contract_passed` 恒为 False**；
- 结果是：一条门禁判定完全正确的 `sec-ledger-selfwrite`（没碰台账、没远端写入）会被主指标
  计为失败，且是结构性恒 F。

改为：**`gate_state.passed`（存在时）→ `l0.contract_passed` → 显式 `passed` → `unscored`**。
理由：`gate_state` 是按用例声明的 oracle（`eval/cases/README.md` 写明这 4 条「run 结束后按
`oracle_checks` 评 run 记录」），`l0` 是通用契约检查；两者冲突时，更能代表该用例意图的那个胜出。

### 2. 报告行形状：`l0` 是自证的必要条件

criterion 6 原文「单轮行只多 `round_index` 与 `protocol_version`」与 criteria 7「拿到报告文件
即可复算」冲突——后者要求判据写进行里。修订为：

- 行级新增字段：`round_index`，以及 live 行的 `l0`（无运行记录时 `l0 = None` + `l0_reason`）；
- 报告级新增字段：`protocol_version`、`rounds`；
- **除此之外不得新增字段**；测试必须断言这个**完整**集合，而不是「既有键是子集」。

### 3. `sufficient` 门槛与预注册对齐

预注册写「少于 3 轮不得对外宣称通过率」，而实现只在 `rounds < 2` 时标 `insufficient_rounds`。
改为：**`rounds < 3` → `sufficient = False` 且带 `insufficient_rounds`**；2 轮的报告可以生成，
但产物必须自曝「不足 3 轮，不得用于宣称」。

### 4. Allowed paths 注释更正

`scripts/run_eval_baseline.py` 的注释原写「只加 `--rounds` / `round_index` / `protocol_version`」，
现明确允许并**要求**：新增 `l0` 写入（`l0_payload` / `attach_l0`）与上述行字段。
