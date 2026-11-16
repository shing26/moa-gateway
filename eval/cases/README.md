# 评测集用例（策展数据，非交付）

本目录是 S1 预注册实验的测量语料：26 条 `EvalCase`（`eval/schema.py`），按
issue #80 的落地顺序第 2–3 步策展而成。**这些文件是数据，不经过治理交付管道**；
守护它们的是 `tests/unit/test_eval_cases.py`（可加载性 + 可复现性不变量）。

## 构成

| 文件 | 类 | 条数 | 来源 |
|---|---|---|---|
| `business.jsonl` | business | 8 | 台账交付的 goal 原文复盘：goal 取自该次运行的 `request.goal`（来源 run 可能是该 goal 的某次尝试，不保证是最终成行那一次） |
| `failure.jsonl` | failure | 8 | 台账中失败形状的复盘：同一次 goal，但记录的是失败的运行 |
| `boundary.jsonl` | boundary | 5 | 合成；每条在 `source` 挂真实锚点（代码分支 / issue / 台账行） |
| `security.jsonl` | security | 5 | 合成；每条锚定一条常设治理规则（`.env` 禁改、人工门禁、台账人签等） |

## 策展规则（生成与裁决的东西不能混）

1. **goal 原文不改写**：business/failure 的 `goal` 逐字复制该次运行的 `request.goal`。
   改写 goal 会让评测测到策展人的文笔而不是被测 agent 的行为。
2. **`base_sha` 取该次运行的基线**：用例在真实历史基线上重放，不在 HEAD 上重放。
3. **`allowed_paths` 取该次运行 bundle 的 added/modified 文件**：这是「这次任务
   真实的写范围」，不是事后理想化的范围。
4. **`verify_commands` 取该次运行 bundle 的 verify 命令**（argv 以空格连接）。
   **bundle 为 null 的失败运行**（从未进入 CHECKING）：写范围与验证命令改取同一
   goal、同一 `base_sha` 的成行运行 bundle——否则失败用例没有可复现的写范围。
   这条派生已由独立 verifier 逐条核对相等。
5. **business/failure 必须能指到运行记录**：`source` 里带 `dlv-<32 hex>`；测试
   会检查这一点——没有运行记录的依据就不是复盘，只是想象。
6. **boundary/security 必须带 oracle 检查项**：合成用例不重放运行，靠 gate_state
   判定，所以 `oracle_checks` 是它的全部判据。

## 9 条合成用例的可测路径（`plans/eval-gate-state-v1.md`）

基线 v1 只测到 17 条：9 条没有 verify 命令，`app/cli.py` 在构造请求前就以
exit 2 拒绝。现在这 9 条各有且只有一条路径，用两个标签区分：

| 标签 | 条数 | 怎么测 | verify 命令 |
|---|---|---|---|
| `gate-state-live` | 4 | 语料**声明**一条中性 verify 命令，走真实 `delivery run`；run 结束后按 `oracle_checks` 评 run 记录 | `python -c "print('gate-state-declared-verify')"` |
| `gate-state-probe` | 5 | `eval/gate_state.py` 进程内直接构造门禁；不调模型、不联网、不需要 Postgres | 仍为空 |

- 中性命令是语料声明，不是运行器注入。它不参与判定（判据是 `oracle_checks`）。
- 它刻意不等于注入载荷里的 `python -c pass`；否则“verify 命令未被改写”就无法
  与“本来就是这个值”区分。
- live 用例的路径证据同时取 bundle 与 run 记录里的工具调用；`.env` 被根
  `.gitignore` 忽略，只看 bundle 会漏掉它。

## 语料不在仓库里

运行记录（`data/delivery-runs/dlv-*.json`）不入库。用例只保留重放所需的字段，
因此它们自带全部依据：goal、基线、写范围、验证命令、来源 run id。

**因此仓库内测试只守结构不变量**（id 唯一与前缀、base_sha 形状、必需字段非空、
business/failure 必须指到 `dlv-` 记录）。「goal 是否真逐字」「base_sha 是否真是
该次运行的基线」这类语料等价性，只能靠评审时对照运行记录核验——改动入库前由
独立 verifier 逐条核对（16 条复盘用例全部 exact）。新增或修改用例时请照做。

## 下一步（第 4 步，未开工）

评测协议先注册成 issue，再跑基线——先注册后开测，避免「跑完再挑指标」。
