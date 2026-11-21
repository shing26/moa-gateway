# 契约：failed / budget_exhausted 一键续跑（retry-failed-run）

- **task id**：`retry-failed-run-20261121`
- **分支**：`feat/retry-failed-run-20261121`
- **背景**：`delivery resume` 现只认两个分支（`resume.py` `_GATE_REENTRY_STATE` 与
  `_RECONCILE_STATES`）；`FAILED` / `BUDGET_EXHAUSTED` 的记录无法续跑，失败只能
  整单重来。路线图阶段 4 的「失败→一键续跑」。
- **所有者签收**：用户消息「继续」（2026-11-21，自用优先主线第 ③ 项）。

## 1. 语义（新分支，不是门禁重入）

`delivery resume --run-id X` 命中 **FAILED / BUDGET_EXHAUSTED** 且失败码可续跑时，
**新开一单**：从父记录的 `DeliveryRequest` 复制请求、全新 `run_id` + `trace_id`、
预算从 settings 重新取满，走与 `delivery run` 完全相同的 DeliveryFlow
（implementer → 验证 → 门禁 → publish）。**不是**跳过任何门禁——新单照常过
推送审批，审批人 ≠ 操作者的既有规则不变。

- **可续跑失败码**（父记录未被推送过、或重跑是唯一合理出路）：
  `NO_CHANGES`、`VERIFY_FAILED`、`VERIFIER_INVALID`、`VERIFIER_REVISE_EXHAUSTED`、
  `PREFLIGHT_FAILED`、`BUNDLE_CHANGED`、`INFRA_ERROR`（`BUDGET_EXHAUSTED` 态
  恒带此码）。
- **拒绝续跑（fail-closed，exit 2 + 理由）**：
  - `PUSH_FAILED`（推送可能已部分发生，PR 可能已存在，须人判）；
  - `CI_FAILED` / `MERGE_SHA_MISMATCH` / `MERGE_NOT_MERGEABLE` / `MERGE_FAILED`
    （已有 PR，走 ci-triage 路径，不走重跑）；
  - `APPROVAL_REJECTED`（人的决定，自动重跑 = 绕过人审；状态门天然排除
    `REJECTED` 态，代码层面再兜一层）；
  - 其余状态（含 `NEEDS_HUMAN`、进行中各态）维持现有 refusal 行为不变。

## 2. 续跑单的 provenance（不新增 schema 字段）

- 新 `DeliveryRequest` = 父请求复制 + `source_ref = "retry:<父 run_id>"` +
  **全新** `trace_id`；父记录一个字节不改（历史是事实）。
- 新 run 经 DeliveryFlow 自带审计（delivery_lifecycle 行）落盘，`source_ref`
  随 request 进 Run Record——血缘可查，无需新表。

## 3. 配置与预算

- verify_command / base_branch **现场重解析**（`resolve_delivery_config`）：
  父单失败后修好的档案立即生效；解析失败或 verify_command 为空 → exit 2
  （preflight 的 verify 检查已能提前挡住坏档案）。
- `delivery_max_steps` / `delivery_max_tokens` / `change_agent_model` 从
  settings 取当前值——「预算用尽重跑」的意义就在于预算重置。

## 4. 允许路径（Allowed paths）

```
apps/code_review_pipeline/delivery/retry.py        （新建，续跑主逻辑）
apps/code_review_pipeline/delivery/resume.py       （仅状态分派：把 FAILED/BUDGET_EXHAUSTED 分给 retry）
app/cli.py                                          （仅 cmd_delivery_resume 及其参数传递/输出）
tests/unit/test_delivery_resume.py                  （追加用例）
tests/unit/test_delivery_retry.py                   （新建）
plans/retry-failed-run-20261121.md
```

**明确不做**：`cmd_delivery_run` 的 execute 块零改动（retry.py 自建同构栈，
宁可 ~60 行受测重复也不动黄金路径）；`flow.py` / `models.py` / run store 零改动。

## 5. 验收标准

- **R1 分派**：`FAILED`+`VERIFY_FAILED` 与 `BUDGET_EXHAUSTED`+`INFRA_ERROR`
  进入 retry；新 run 走完整 flow（测试用注入的 fake flow factory 断言请求内容：
  goal/title/branch/repo 与父一致、`source_ref="retry:<父id>"`、trace_id ≠ 父、
  run_id ≠ 父）。
- **R2 拒绝面**：`FAILED`+`PUSH_FAILED` / `CI_FAILED` / `MERGE_FAILED` /
  `APPROVAL_REJECTED` → exit 2、reason 说明对应理由、**不创建新 run**、父记录不变。
- **R3 既有行为不变**：`ready_for_push_approval` 门禁重入、三态 reconcile、
  其余状态 refusal 全部照旧（既有测试全绿）。
- **R4 配置 fail-closed**：档案缺失 / verify_command 空 → exit 2，不建新 run。
- **R5 锁**：retry 走与 `delivery run` 同一把 `(record_dir, repo)` 锁；锁被活
  进程持有 → exit 3，不建新 run。
- **R6 退出码**：新单 `waiting_ci` / `ready_for_push_approval` → 0；
  新单再失败 → 1；拒绝/配置错 → 2；锁冲突 → 3。CLI 打印父与新 run_id。
- **R7 硬规则**：不新增 `# noqa: BLE001`（具名异常直接捕获）；不新增 skip/xfail；
  不改既有断言。

## 6. 验证命令

```
V1  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit/test_delivery_retry.py tests/unit/test_delivery_resume.py -q
V2  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m ruff check .
```

外加独立探针（Verifier 自写）：fake store + fake flow factory，覆盖 R1 两种
命中、R2 至少三种拒绝、R4 空配置、trace_id/source_ref 断言；贴脚本与输出。

## 7. 禁止

- 绕过推送审批（新单必须过同一个 `PushApprovalPort`）。
- 复用父 run_id / 父 trace_id；改写父记录任何字段。
- 对 `APPROVAL_REJECTED` / `PUSH_FAILED` / CI/MERGE 类失败码放行。
- 动 `cmd_delivery_run`、`flow.py`、`models.py`、run store、审计语义。
- 触碰 Allowed paths 之外的模块。

## 8. 签收记录（2026-11-21）

**所有者书面签收**：用户消息「签收」（2026-11-21，指 PR #121）。

**合并**：PR #121 → `main`，merge commit
`0f2ccde019ca6ccd8a953cc4c9120648c86ce25e`，2026-11-21T08:47:59Z。

**验收证据**

- 实现 HEAD：`667595eaa527097003314fe6f02dac01af3f9e6e`；`git status --short` 空。
- V1 `29 passed`；V2 全量分块 `2280 passed / 0 failed`（首跑 1 个 error 经三层
  复跑证明为环境性 WinError 10055 socket 缓冲耗尽，非代码归因）；
  V3 `All checks passed!`。
- 独立 Verifier 自写探针 41 checks / 0 failed；R1–R7 全 PASS；无 P0/P1。
- `cmd_delivery_run` diff 0 行；`flow.py` / `models.py` / run store 零改动。
- GitHub CI：`test` / `integration` 绿，`docker` skipped。

**人审四问**

1. **是否在冻结契约范围内？** 是。6 个文件全部在 Allowed paths；
   `cmd_delivery_run` / `flow.py` / `models.py` / run store / 审计语义零改动。
2. **证据是否绑定 HEAD 且可复跑？** 是。V1–V3、探针、循环导入探针由
   独立 Verifier 亲跑，未采信实现方自评。
3. **是否有未声明行为变化？** 无。新分支只命中 `FAILED` / `BUDGET_EXHAUSTED`
   + 白名单失败码；门禁重入与 reconcile 分支及其余 refusal 逐字不变；
   新单照常过 `PushApprovalPort`。
4. **残余是否如实记录并接受？** 是。4 条 P2（delivery→cli 反向依赖、过期锁
   静默抢占无警告、`flow_factory` 测试注入缝、CLI 打印无 CLI 层测试）
   全部记录在验收报告，不阻塞。
