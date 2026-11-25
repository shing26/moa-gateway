# 契约：合并通道的 Postgres 连接自愈

- **task id**：`merge-store-reconnect-20261125`
- **分支**：`fix/merge-store-reconnect-20261125`
- **背景**：2026-11-25 03:31，`moa-gateway-postgres-1` 容器被停掉又拉起
  （Docker Desktop 更新失败的连带影响）。网关与 `app.merge_worker` 都是长驻
  进程，`PostgresMergeStore._connect()` 只在 `self._conn is None` 时建连，
  于是旧连接句柄一直留着。恢复后的实际症状：

  ```
  psycopg.OperationalError: the connection is closed
    at merge_store.py list_open() -> self._conn.cursor()
  ```

  worker 每 60 秒抛一次、空转，直到人工重启进程。这不是环境偶发：任何
  Postgres 重启、容器重建、网络瞬断都会把这个长驻进程卡死在旧句柄上。

## 1. 语义

### R1 已关闭的连接要重建

`PostgresMergeStore._connect()` 增加「句柄存在但已关闭」的分支：

- `self._conn is None` → 建连（现有行为不变）；
- `self._conn` 存在且 `closed` 为真 → 先尽力关闭旧句柄（关闭失败只忽略，
  不上抛），置 `None`，再建连一次；
- `self._conn` 存在且未关闭 → **不做任何事**（不得在健康连接上重连，
  否则会打断正在进行的事务）。

`closed` 用 `getattr(conn, "closed", False)` 读取：psycopg3 的
`Connection.closed` 在底层连接断开后为真，假实现（单测）也能按属性模拟。

### R2 不在调用点加重试

连接重建只发生在 `_connect()`。各方法（`save` / `get` / `transition` /
`list_open` / `supersede_older` / `set_card_message_id`）的
`self._connect()` 调用位置不变，异常语义不变：重建失败照常上抛，不吞异常、
不睡眠重试。worker 的下一轮轮询天然就是重试。

### R3 边界

- 不新增依赖、不引入连接池、不改 DSN / `connect_timeout`。
- 不改任何 SQL、`DO UPDATE` 白名单或状态机。
- `close()` 的语义不变（关闭后 `self._conn = None`，下次调用重建）。

## 2. 允许路径

```
apps/code_review_pipeline/merge_store.py
tests/unit/test_merge_store.py
plans/merge-store-reconnect-20261125.md
```

## 3. 验收标准

- **T1** `_connect()` 在句柄已关闭时调用一次 `connect()`，并按
  `dsn` + `connect_timeout` 传入；旧句柄的 `close()` 被调用一次。
- **T2** 句柄健康时（`closed` 为假）重复调用 `_connect()` 不产生第二次
  `connect()`。
- **T3** 旧句柄 `close()` 抛异常时仍能完成重建（异常被吞），且新句柄可用。
- **T4** 重建时 `connect()` 抛异常照常上抛（不吞），`self._conn` 不被
  赋成半个连接。
- **T5** 既有 `PostgresMergeStore` 测试全绿，SQL 断言不变（`save` 冲突
  分支仍只有 `updated_at`，`set_card_message_id` 仍只写一列）。
- **T6** 不新增依赖；不删弱化断言；`ruff check .` 全绿。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_merge_store.py tests/unit/test_merge_worker.py tests/unit/test_merge_poller.py tests/unit/test_merge_executor.py tests/unit/test_merge_store_wiring.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

解释器一律 `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`。

## 5. 禁止

- 在 `_connect()` 里加循环重试、退避或 sleep（会拖住 webhook 请求线程）。
- 在健康连接上无条件重连（打断事务、丢失 `FOR UPDATE` 的锁语义）。
- 吞掉重建失败让调用方拿到 `None` 连接。
- 顺手改 `storage/review_store.py`（同款模式，但不在本单 Allowed paths；
  另行立契）。
- 新增依赖；改动 Allowed paths 之外的文件。

## 6. 签收记录

**所有者签收**：飞书审批卡「批准」，2026-11-25 14:30:10（+08），
operator `ou_ae3e5cc009fae3462147e15e8c271602`。

**合并**：PR #144 → `main`，merge commit
`e3ddf8ffde5b8d6d784be5580d7244091f4a3a7b`，`merging -> merged (complete)`
于 14:30:15。

**验收证据**：独立 Verifier `ACCEPT_WITH_NITS`（HEAD `c3f7448`），T1–T6
全 PASS，P0/P1 无。V1 `50 passed`；V2 `2387 passed, 6 skipped`；V3
`All checks passed!`。GitHub CI：`test` / `integration` 绿，`docker`
skipped。

## 7. 已知同类缺口（不在本单范围）

`apps/code_review_pipeline/storage/review_store.py` 的 `PostgresReviewStore`
是同一份 `if self._conn is None` 写法，容器重启后同样会卡住审查通道。
本单只修合并通道，审查通道另立契处理——避免一次改动跨两个高流量模块。
