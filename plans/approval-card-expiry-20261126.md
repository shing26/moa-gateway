# 契约：合并审批卡过期自愈

- **task id**：`approval-card-expiry-20261126`
- **分支**：`fix/approval-card-expiry-20261126`
- **背景**：2026-11-25 实测。PR #146 的合并审批卡于 15:25 发出，HITL 记录
  （`moa:hitl:<task_key>`）按 `SessionStore` 默认 TTL 3600s 于 16:25 过期。
  卡片消息本身一直在飞书会话里、按钮也还在，但 17:30 之后点下去只会落到
  「该审批已失效」。当天 #146 因此白等了一轮，最后靠人工重发才继续。

  这不是展示问题：`awaiting_approval` 记录会一直留在库里，worker 每轮照常
  轮询，却没有任何机制发现「这张卡已经点不动了」。人只能靠猜。

## 1. 语义

### R1 过期检测

`MergeWorker._run_once` 每轮在现有轮询之后执行一次过期巡检：

- 只处理 `store.list_open()` 中 `state == awaiting_approval` 的记录；
- `card_message_id` 为空的记录跳过（无法定位旧卡，也避免无卡时反复打扰）；
- 用 `SessionStore.get_hitl(task_key)` 判断登记是否还在：
  - 返回 `HitlRequest` → 审批仍有效，跳过；
  - 返回 `None` → 视为过期，进入 R2；
  - 抛异常（Redis 不可达等）→ 记 warning 后跳过本轮，**不得**据此重发。

「返回 None」是唯一重发信号。异常与 None 必须分开：Redis 抖动时重发会变成
每轮刷一张新卡的循环。

**但是**（2026-11-26 Verifier 实测修订）：`REDIS_ENABLE_FALLBACK=true`（默认且
本机真实配置）下，Redis 不可达不会抛异常——`RedisHitlStorage` 会降级为进程内存，
`get()` 返回内存副本里的 `None`。这个 `None` 与「登记真的过期」不可区分，直接
重发会在 Redis 故障期间每轮换一张卡，而且降级期间新登记的 HITL 只存在于 worker
进程内存里，网关认领不到，新卡同样点不动。

因此新增前置条件：**HITL 存储必须处于「跨进程权威」状态才允许判定过期**。
`SessionStore` 暴露一个只读 `using_memory` 属性（`_storage is None` 或底层
`RedisHitlStorage.using_memory` 为真 → `True`）；巡检在读取任何记录前先检查它，
为真则记 warning 并整轮跳过。判定一律用 `getattr(..., False)` 读取，使测试替身
（无该属性）按「权威」处理。

### R2 过期处置

对判定为过期的记录：

1. 把旧卡改成无按钮终态：新增 outcome `expired`，文案
   `⏳ 审批已过期，已重发新卡`（`merge_resolved_failed` 标题族，不改已有
   `merged` / `rejected` / `superseded` 文案）；
2. 重新走 `send_approval_card`：重新登记 HITL + 发一张新卡；
3. 发送成功 → `store.set_card_message_id(identity, 新 message_id)`；
4. **记录状态保持 `awaiting_approval`**。

第 4 条是硬约束：`MergeState.FAILED` 是终态（`ALLOWED_ACTIONS[FAILED]` 为空），
一旦把过期记录转成 `failed`，后续 CI 再绿也没有任何动作能把它拉回来。

### R3 边界

- 不改 TTL、不改状态机、不改 SQL / schema、不新增依赖。
- 不改推送审批（`delivery/feishu_approval.py`）路径；本单只覆盖合并审批卡。
- `send_resolved_card` / `send_approval_card` 任一失败只记日志，不中断 worker
  轮询；记录保持原状态，下一轮再试。
- 旧卡改卡失败不阻止发新卡：改卡是展示层，新卡才是可操作对象。

## 2. 允许路径

```
app/merge_worker.py
app/engine.py
apps/code_review_pipeline/notifications/merge_notifier.py
tests/unit/test_engine_hitl.py
tests/unit/test_merge_worker.py
tests/unit/test_merge_notifier.py
plans/approval-card-expiry-20261126.md
```

## 3. 验收标准

- **T1** `awaiting_approval` + 有 `card_message_id` + `get_hitl()` 返回
  `None`：旧卡收到 `expired` 终态改卡；发出新卡；`set_card_message_id`
  写入新 id；记录状态仍为 `awaiting_approval`。
- **T2** `get_hitl()` 返回 `HitlRequest`：不改卡、不发卡、不写 id。
- **T3** `get_hitl()` 抛异常：不改卡、不发卡，异常不外泄，同一轮其他记录
  仍被处理。
- **T4** `card_message_id` 为空：跳过，不调用改卡/发卡。
- **T5** 非 `awaiting_approval`（`watching` / `merged` / `failed`）：
  跳过，不调用改卡/发卡。
- **T6** `expired` 文案渲染为 `⏳ 审批已过期，已重发新卡`，且
  `merged` / `rejected` / `superseded` / 默认 failed 文案不变。
- **T7** 既有 `test_merge_worker.py` / `test_merge_notifier.py` 全绿；
  不删弱化断言；无新增 skip；`ruff check .` 全绿；无新依赖。
- **T8** HITL 存储降级（`SessionStore.using_memory is True`，或底层
  `RedisHitlStorage` 已 fallback）时：即使 `get_hitl()` 返回 `None`，也不改卡、
  不发新卡；`SessionStore.using_memory` 能正确转发底层状态，`_storage is None`
  时为 `True`。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_merge_worker.py tests/unit/test_merge_notifier.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

解释器一律 `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`。

## 5. 禁止

- 把过期记录 transition 成 `failed` / `rejected` / `superseded` 任一终态。
- 在 `get_hitl()` 抛异常时重发（会把 Redis 抖动放大成刷屏）。
- 无 `card_message_id` 时发新卡。
- 在 worker 轮询里加 sleep / 退避 / 循环重试。
- 改 TTL、改 SQL、改状态机、动 `delivery/feishu_approval.py`。
- 新增依赖；改动 Allowed paths 之外的文件。

## 6. 签收记录

（待所有者书面签收）
