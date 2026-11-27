# 契约：交付状态机 abandon（人工终结）语义

- **task id**：`delivery-abandon-20261127`
- **分支**：`feat/delivery-abandon-20261127`
- **背景**：`delivery stats` 报 `in_flight 64`，其中一类是永远出不来的人造残局：
  `dlv-5c400b831c8047d6a6af7cdc3b228c84`（OpsShield PR #110）停在 `waiting_ci`，
  对应的 PR 早已被关掉，CI 再也不会出结论；状态表里 `WAITING_CI` 只有
  「CI 绿 → 等合并审批」和「→ needs_human」两条出路，没有任何一条通向「别等了」。
  于是记录永久占着 `in_flight`，合并卡片（若有）也一直可点。

  当前可用的动作只有 `resume`（要求 `ready_for_push_approval`，或对三态做合并对齐）
  与 `retry`（只认 `failed` / `budget_exhausted`）。人想「这单不做了」时无路可走。

## 1. 语义

### R1 状态与失败码

- 新增 `DeliveryRunState.ABANDONED = "abandoned"`，**终态**：
  `ALLOWED_TRANSITIONS[ABANDONED] == frozenset()`。
- 新增 `FailureCode.ABANDONED = "abandoned"`。
- 允许 abandon 的来源状态：`preparing`、`implementing`、`checking`、
  `verifier_review`、`revising`、`ready_for_push_approval`、`pushing`、
  `waiting_ci`、`awaiting_merge_approval`、`needs_human`、`failed`、
  `budget_exhausted`。逐条在 `ALLOWED_TRANSITIONS` 里加上 `ABANDONED` 目标。
- 拒绝的来源状态：
  - `merging`：已经交给合并执行器，远端写可能已发出，`abandon` 不能替这次
    调用背书。
  - `merged`：已合并的交付不是「放弃」，是完成。
  - `rejected`：人工拒绝也是终态决定，重复终结只会污染历史。
- `abandoned` 再 abandon：**幂等**——不写记录、不写审计、exit 0，
  打印 `already abandoned`。

### R2 并发与独占

- 与 `delivery run` / `retry` 同一把锁、同一路径规则：
  `<delivery_run_record_dir>/.locks/delivery-<repo 的 / 换成 ->.lock`。
- 拿不到锁（持有者进程活着）→ 拒绝，exit 3，打印 holder 的
  `pid / started_at / host`。理由：`implementing` 等状态可能对应活着的进程，
  先终结记录再被进程覆盖会写出自相矛盾的记录。
- 返回 `StaleLockReplaced`（持有者已死、锁被抢走）→ 打印 warning 后继续，
  与 `retry` 的既有行为一致。
- 锁在 `finally` 中释放。

### R3 记录写入

- `reason` 必填：`strip()` 后为空 → 拒绝，exit 2，**不落盘**。
- 成功终结写一条新记录：
  `replace(run, state=ABANDONED, failure=FailureInfo(ABANDONED, reason), updated_at=now)`。
- 记录里的 `bundle` / `verification` / `push` / `merge` / `attempts` 一个字段
  都不删——abandon 是给状态收口，不是删证据。**记录文件本身不删。**
- 不碰远端：不删分支、不关 PR、不评论、不发任何 GitHub 请求。

### R4 合并通道联动

`abandon` 之后那张合并审批卡必须点不动，否则「本地已放弃、卡片还能点」是
比现状更糟的状态。

- 若 `run.push is None`：没有卡片，直接按 R3 写记录。
- 若 `run.push is not None`：
  - 必须拿到共享 `MergeStore`（`PostgresMergeStore`）。拿不到 → 拒绝，exit 2，
    文案要求先配置 `VECTOR_DB_DSN` / `CODE_REVIEW_DATABASE_URL`；**不得**
    只改本地记录。
  - 按 `identity = (repo, pr_number, head_sha)` 查记录：
    - 不存在 → 继续（没有卡片要收）。
    - 非终态（`watching` / `ci_failed` / `awaiting_approval`）→
      `transition(identity, MergeAction.SUPERSEDE, at=..., failure_reason=reason)`。
    - 已是 `superseded` / `rejected` / `failed` → 不动，继续。
    - `merged` → 拒绝，exit 2，提示先 `delivery resume --run-id ...` 把运行
      对齐成 `merged`；不得把已合并的 PR 记成 abandoned。
    - `merging` → 拒绝，exit 2（与 R1 的 `merging` 拒绝同源：远端写可能已发出）。
- **顺序硬约束**：先 supersede 合并记录，成功后才写 `ABANDONED`。
  supersede 失败 → 整单拒绝，记录保持原状态。反过来会留下可点的卡片。
- 失败路径上 supersede 已成功、写记录失败时的残留状态（记录 superseded、
  run 未变）是可接受方向：卡片已死，人工重跑 `abandon` 仍能收口。

### R5 与 resume / retry 的互斥

- `delivery resume` 对 `abandoned`：拒绝，exit 2，文案点名
  `abandoned` 是终态、不会重入。
- `retry_failed_run` 对 `abandoned`：拒绝（`RETRY_STATES` 不含 `ABANDONED`），
  现有「run state is abandoned; retry accepts only failed / budget_exhausted」
  文案即可，不新增分支。
- `DeliveryRunStore` 的 `_TERMINAL_RUN_STATES` 加入 `ABANDONED`：
  合并轮询（`sync` / `reconcile`）不得把 abandoned 拉回 `needs_human` /
  `merged`。这是 R1「终态」在存储层的落地。

### R6 审计

- 成功终结写一条 `WalDeliveryAudit`：`previous=<原状态>`、
  `current=ABANDONED`、`detail={"reason": ..., "operator": ..., "merge_record": ...}`，
  其中 `merge_record` 取 `"superseded"` / `"none"` / `"terminal:<state>"`。
- 幂等 no-op 不写审计（避免哈希链里出现重复的「终结」）。
- 顺序：supersede → 写记录 → 写审计。审计写入失败 → exit 1，打印
  「记录已终结、审计写入失败」，并输出需要补写的 `run_id`；不回滚记录。

### R7 CLI

```
python -m app.cli delivery abandon --run-id <id> --reason "..." [--json]
```

- 文本输出：`run_id` / `state` / `failure` / `pr`（如有）/ `merge_record`。
- `--json`：输出 outcome 字典，字段与文本一致。
- 退出码：`0` 成功或幂等；`1` 记录已写但审计失败；`2` 拒绝（状态、
  merge 记录、DSN、空 reason）；`3` 锁被活进程持有。
- `delivery abandon` **不要求** `GITHUB_TOKEN`（它不碰远端）；只有
  `run.push` 存在时才要求 MergeStore。

### R8 统计口径（`delivery stats`）

- 新增 `abandoned` 计数（来自 `state == "abandoned"`）。
- `abandoned` **不计入** `failed`、**不计入** `in_flight`。
- `success_rate` 的分母保持 `delivered + failed`（即 abandon 不计入成功率，
  也不拉低它），docstring 写明这条口径。
- `by_state` 保留 `abandoned` 的计数，便于审计「有多少单是被人工放弃的」。

### R9 每日摘要（`delivery digest`）

- `abandoned` 不进入 `STUCK_DELIVERY_STATES`、`PENDING_DELIVERY_STATES`、
  `IN_PROGRESS_DELIVERY_STATES`：已收口的单不该每天在摘要里催人。
- 本单不改 `digest.py` 的任何文案与发送逻辑；只要 `abandoned` 不被三张
  状态集合收录即可（现有集合是字面量，天然不收录新状态，测试需固化这一点）。

## 2. 允许路径

```
apps/code_review_pipeline/delivery/models.py
apps/code_review_pipeline/delivery/state.py
apps/code_review_pipeline/delivery/run_store.py
apps/code_review_pipeline/delivery/abandon.py
apps/code_review_pipeline/delivery/resume.py
apps/code_review_pipeline/delivery/stats.py
app/cli.py
tests/unit/test_delivery_state.py
tests/unit/test_delivery_abandon.py
tests/unit/test_delivery_store.py
tests/unit/test_delivery_stats.py
tests/unit/test_delivery_cli.py
tests/unit/test_delivery_resume.py
tests/unit/test_delivery_retry.py
plans/delivery-abandon-20261127.md
```

## 3. 验收标准

- **T1** `abandon_run` 从 `waiting_ci` 成功：记录 `state == abandoned`、
  `failure.code == abandoned`、`failure.message == reason`；原有 `push` /
  `bundle` 字段逐字段不变；`updated_at` 前进。
- **T2** 空 reason（`""`、`"   "`）→ 拒绝，记录一个字节不变，exit 2。
- **T3** 来源状态 `merging` / `merged` / `rejected` → 拒绝，exit 2，
  记录不变；`abandoned` → 幂等 exit 0，记录不变、审计不新增。
- **T4** `run.push` 存在且合并记录为 `awaiting_approval` →
  该记录变 `superseded`；`list_open()` 不再包含它；本地记录变 `abandoned`。
- **T5** `run.push` 存在且合并记录为 `merged` → 拒绝，exit 2；
  合并记录与本地记录都不变。
- **T6** `run.push` 存在但 MergeStore 不可用（非 Postgres）→ 拒绝，exit 2，
  本地记录不变。
- **T7** 锁被活进程持有 → 拒绝，exit 3，记录不变；`StaleLockReplaced`
  路径继续执行并打印 warning。
- **T8** 成功路径写一条 WAL 审计，`from_state` 为原状态、`to_state` 为
  `abandoned`、`detail.reason` 为原 reason；审计抛错时 `abandon_run`
  返回 exit 1 且记录已是 `abandoned`。
- **T9** `DeliveryRunStore` 对 `abandoned` 记录做 `sync` / `reconcile`
  （包括合并记录为 `merged` / `superseded`）→ 状态保持 `abandoned`。
- **T10** `retry_failed_run` / `resume_run` 对 `abandoned` 均拒绝，exit 2。
- **T11** `collect_stats`：1 merged + 1 failed + 2 abandoned →
  `abandoned == 2`、`failed == 1`、`in_flight == 0`、
  `success_rate == 0.5`；`by_state["abandoned"] == 2`。
- **T12** `delivery abandon` CLI：成功打印 `state: abandoned` 且 exit 0；
  拒绝时 exit 2 且 stderr 有 reason；`--json` 输出可 `json.loads`。
- **T13** 既有 `tests/unit` 全绿，不删弱化断言、无新增 skip；
  `ruff check .` 全绿；无新依赖。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_delivery_abandon.py tests/unit/test_delivery_state.py tests/unit/test_delivery_store.py tests/unit/test_delivery_stats.py tests/unit/test_delivery_cli.py tests/unit/test_delivery_resume.py tests/unit/test_delivery_retry.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
V4  （人工，签收后）$env:PYTHONPATH='.'; python -m app.cli delivery abandon --run-id dlv-5c400b831c8047d6a6af7cdc3b228c84 --reason "OpsShield PR #110 已关闭，CI 不会再有结论"
```

解释器一律 `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`。

## 5. 禁止

- 删远端分支、关 PR、发评论，或以任何形式写 GitHub。
- 删除 delivery 记录文件、清空 `bundle` / `verification` / `push` 证据。
- 在 MergeStore 不可用时只改本地记录（会留下可点的卡片）。
- 把 `merged` / `merging` 的交付记成 `abandoned`。
- 让 `abandoned` 记录被合并轮询改写，或出现在 `in_flight` / 每日摘要的
  「卡住」分区。
- 新增依赖；改 SQL / schema；改 TTL；动 `delivery/feishu_approval.py`、
  `app/merge_worker.py`、`merge_notifier.py`。
- 改动 Allowed paths 之外的文件。

## 6. 签收记录

（待所有者书面签收）
