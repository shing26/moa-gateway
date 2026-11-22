# 契约：review 修正（docs / tests / 两处小重构）

- **task id**：`review-fixes-20261122`
- **分支**：`fix/review-fixes-20261122`
- **触发**：`caebef5...3e73207` 双轴 review 的 P1/P2 发现（Standards 2 硬违规 + 9 判断项；
  Spec 6 条）。功能本身不需返工，本单只做纠偏。
- **所有者签收**：用户消息「开修正 PR」（2026-11-22）。

## 1. 要修什么（逐条对应 review 发现）

- **F1 文档与行为矛盾**：`apps/code_review_pipeline/delivery/resume.py:6` 与
  `app/cli.py:865` 仍宣称「绝不重跑实现者」；实际新增的 FAILED/BUDGET_EXHAUSTED
  分支会经 `retry_failed_run` 重跑实现者。文档必须改成准确表述。
- **F2 测试覆盖被删**：`tests/unit/test_delivery_resume.py` 的
  `test_gate_reentry_state_is_only_ready_for_push_approval` 删除了
  `FAILED` / `BUDGET_EXHAUSTED` 两行（违反 `plans/retry-failed-run-20261121.md` R7
  与 §4「仅追加」字面）。修复方式：这两条状态改为**显式断言分派到 retry**
  （而非从覆盖中消失），其余 9 个状态的 refusal 断言逐字保留。
- **F3 字符串枚举**：`apps/code_review_pipeline/delivery/retry.py:113`
  `run.state.value in {"waiting_ci", "ready_for_push_approval"}` → 用
  `DeliveryRunState` 成员集合。
- **F4 topology 来源**：`retry.py:174` `topology=parent.topology` →
  `settings.delivery_topology`，与 `delivery run` 同源；非 `single` 时
  fail-closed（exit 2，不建新 run）。
- **F5 契约补记**：
  - `plans/retry-failed-run-20261121.md` 追加「修正案 1」：记录
    `flow_factory` 测试注入缝、`CHANGE_AGENT_MODEL` 为空 → exit 2、
    topology 改 settings 来源；
  - `plans/audit-chain-rollover-20261120.md` §7 追加已知限制：
    `_resume_chain` 只扫 64KB 尾窗，本写者当日最后一行落在窗外时以空链头起链
    （如实记录，不在本单改实现）。

## 2. 允许路径（Allowed paths）

```
apps/code_review_pipeline/delivery/resume.py     （仅 docstring）
apps/code_review_pipeline/delivery/retry.py      （F3/F4）
app/cli.py                                        （仅 cmd_delivery_resume docstring）
tests/unit/test_delivery_resume.py                （F2）
plans/retry-failed-run-20261121.md                （F5 修正案）
plans/audit-chain-rollover-20261120.md            （F5 已知限制）
plans/review-fixes-20261122.md                    （本契约）
```

## 3. 验收标准

- **F1**：两处 docstring 不再有「绝不重跑实现者」的绝对表述；准确说明
  「门禁重入/合并对齐不重跑实现者；FAILED/BUDGET_EXHAUSTED 经 retry 重跑」。
- **F2**：`FAILED` + 可续跑码、`BUDGET_EXHAUSTED` 两态有显式断言证明分派到
  retry；其余 9 态 refusal 断言逐字不变；测试文件**新增优先**，如必须改旧用例
  则改动仅限这两态且带注释说明（不得弱化其他断言）。
- **F3**：retry 中不再出现 `{"waiting_ci", ...}` 字符串集合；行为等价。
- **F4**：`topology` 取 `settings.delivery_topology`；非 `single` → exit 2 且不建
  新 run，有测试。
- **F5**：两份 plan 的补充如实、可核对，不改既有验收条款。
- **F6**：行为除 F3/F4 外零变化；V1 定向、V2 全量、V3 ruff 全绿；无新增
  `# noqa: BLE001` / skip / xfail。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit/test_delivery_resume.py tests/unit/test_delivery_retry.py -q
V2  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m ruff check .
```

## 5. 禁止

- 改 `retry.py` 的续跑语义（白名单、拒绝面、provenance、审批门）——只做 F3/F4。
- 改 `wal.py` 实现（64KB 只记录，不修）。
- 改既有断言换通过；新增 skip/xfail；触碰清单外模块。

## 6. 修正案 1（2026-11-22，主会话冻结）

**触发**：实现方申报 F4 的必要连带——`retry.py` 改读 `settings.delivery_topology`
后，`tests/unit/test_delivery_retry.py` 的 `SimpleNamespace` fixture 必须补
`delivery_topology="single"`，否则既有成功用例抛 `AttributeError`。该文件原不在
Allowed paths 内。

**裁决**：允许改动 `tests/unit/test_delivery_retry.py`，**仅限**为该 fixture 补
`delivery_topology` 字段（及 F4 新增用例如落在该文件）。不得改该文件既有断言。
其余条款不变。
