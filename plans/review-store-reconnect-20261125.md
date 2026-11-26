# 契约：审查通道的 Postgres 连接自愈

- **task id**：`review-store-reconnect-20261125`
- **分支**：`fix/review-store-reconnect-20261125`
- **背景**：`merge-store-reconnect-20261125`（PR #144）修掉了合并通道的
  旧句柄问题，其契约 §7 明确把审查通道列为已知同类缺口。本单兑现那一项。

  `apps/code_review_pipeline/storage/review_store.py` 的
  `PostgresReviewStore._connect()` 同样只在 `self._conn is None` 时建连。
  网关 / 审查 worker 是长驻进程，`moa-gateway-postgres-1` 重启或重建后，
  旧句柄留在 `self._conn` 上，之后每次 `_connect()` 都直接返回，调用点
  `self._conn.cursor()` 每轮抛：

  ```
  psycopg.OperationalError: the connection is closed
  ```

  审查通道会空转，直到人工重启进程。与合并通道是同一类故障、同一份写法。

## 1. 语义

### R1 已关闭的连接要重建

`PostgresReviewStore._connect()` 增加「句柄存在但已关闭」的分支，语义与
PR #144 的 `PostgresMergeStore` 完全对齐：

- `self._conn is None` → 建连（现有行为不变）；
- `self._conn` 存在且 `closed` 为真 → 先尽力关闭旧句柄（关闭失败只记
  debug 日志、不上抛），置 `None`，再建连一次；
- `self._conn` 存在且未关闭 → **不做任何事**（不得在健康连接上重连，
  否则会打断正在进行的事务）。

`closed` 用 `getattr(conn, "closed", False)` 读取：psycopg3 的
`Connection.closed` 在底层连接断开后为真，单测的假实现也能按属性模拟；
现有假连接没有该属性时按健康处理，保持既有测试语义。

### R2 不在调用点加重试

连接重建只发生在 `_connect()`。`save` / `save_review` / `get` /
`get_task_state` / `_transition` / `claim_task` / `start_review_run` /
`claim_publish_attempt` 等所有调用点的 `self._connect()` 位置不变，异常
语义不变：重建失败照常上抛，不吞异常、不睡眠重试。worker 的下一轮轮询
天然就是重试。

### R3 边界

- 不新增依赖、不引入连接池、不改 DSN / `connect_timeout`。
- 不改任何 SQL、唯一约束、事务边界或任务状态机。
- `close()` 的语义不变（关闭后 `self._conn = None`，下次调用重建）。
- 不改内存实现 `InMemoryReviewStore`。

## 2. 允许路径

```
apps/code_review_pipeline/storage/review_store.py
tests/unit/test_review_store_init.py
plans/review-store-reconnect-20261125.md
```

## 3. 验收标准

- **T1** 句柄已关闭时，`_connect()` 调用一次 `connect()`，并按 `dsn` +
  `connect_timeout` 传入；旧句柄的 `close()` 被调用一次，且不再在旧句柄上
  执行任何 SQL。
- **T2** 句柄健康时（`closed` 为假）重复调用 `_connect()` 不产生第二次
  `connect()`，也不调用 `close()`。
- **T3** 旧句柄 `close()` 抛异常时仍能完成重建（异常被吞），且新句柄可用。
- **T4** 重建时 `connect()` 抛异常照常上抛（不吞），`self._conn` 不会被
  赋成半个连接。
- **T5** 既有 review store 测试全绿，SQL / 状态机断言不变。
- **T6** 不新增依赖；不删弱化断言；`ruff check .` 全绿。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_review_store_init.py tests/unit/test_review_record_projection.py tests/unit/test_task_schema_d2.py tests/unit/test_task_store_contract.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

解释器一律 `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`。

## 5. 禁止

- 在 `_connect()` 里加循环重试、退避或 sleep（会拖住 webhook / worker 线程）。
- 在健康连接上无条件重连（打断事务、丢失锁语义）。
- 吞掉重建失败让调用方拿到 `None` 连接。
- 顺手改 `apps/code_review_pipeline/merge_store.py`（#144 已合并，不在本单范围）。
- 新增依赖；改动 Allowed paths 之外的文件。

## 6. 签收记录

**所有者签收**：飞书审批卡「批准」，2026-11-25 20:35:46（+08），
operator `ou_ae3e5cc009fae3462147e15e8c271602`。

**合并**：PR #146 → `main`，merge commit
`4ac369f25026cd4dd86d480c493e73adca93d6b6`，`merging -> merged (complete)`
于 20:35:51。

**一次失败的合并尝试（留痕）**：首次批准落在 head `be0c1a8`，19:52 合并被
GitHub 以 `Required status check "test" is expected.` 拒绝。根因不是代码或
CI：`strict=true` 要求分支与 base 同步，而 #145 于 17:33 合并使 `main`
前进到 `a121c08`，PR 处于 `BEHIND`。合并 `main` 得到新 head `6908d8a`、
CI 重跑全绿后重新审批，合并成功。旧 head 的批准未被复用——head-SHA 乐观锁
按设计生效。

**验收证据**：独立 Verifier `ACCEPT`（HEAD `be0c1a8`，即合并前的代码
内容；同步 `main` 只引入文档改动，未触碰实现），T1–T6 全 PASS，P0/P1 无。
V1 `43 passed`；V2 `2391 passed, 6 skipped`；V3 `All checks passed!`。把
新测试套在父提交旧实现上 `3 failed, 1 passed`，证明用例能区分新旧行为。
GitHub CI（head `6908d8a`）：`test` / `integration` pass，`docker`
skipping。
