# 契约：推送审批卡过期自愈

- **task id**：`push-approval-card-expiry-20261127`
- **分支**：`fix/push-approval-card-expiry-20261127`
- **背景**：推送审批卡由 CLI 进程发出并阻塞等待（`FEISHU_PUSH_APPROVAL_TIMEOUT_S`，
  本机 1800s）。CLI 一死（Ctrl-C、关终端、崩溃、机器重启），卡片留在飞书里、
  按钮还在，但已经没有进程在等它；HITL 登记按 TTL 3600s 过期后，点下去只得到
  「该审批已失效」。`delivery resume` 重入门禁会发一张新卡，但旧卡继续装得像
  能用。这和 #149 修的合并卡过期是同一类问题：**卡片不会自己说实话**。

  与合并卡不同，推送审批**不能自动重发新卡**：新卡只有正在阻塞等待的 CLI 进程
  才能消费；进程已经死了，重发只会多一张点不动的卡。所以本单的自愈语义是
  **「把旧卡改成诚实的终态，并告诉人下一步跑什么」**，不是重发。

## 1. 语义

### R1 侧车登记（不改 DeliveryRun schema）

`DeliveryRun` 的 `schema_version` 是严格相等校验（`_run_from_dict` 对不上就抛
`unsupported DeliveryRun schema`），现存 200+ 条记录都是当前版本。往 run 记录里
加字段会迫使全量记录失效，**禁止**。

新增侧车登记，与运行记录同目录、独立子目录：

```
<delivery_run_record_dir>/.push-approvals/<hitl_id>.json
{
  "schema_version": 1,
  "hitl_id": "...",
  "run_id": "...",
  "repo": "owner/name",
  "branch": "...",
  "card_message_id": "...",
  "target": "...",
  "created_at": 1764...,
  "expires_at": 1764...,
  "state": "pending"
}
```

- 写入用临时文件 + `os.replace`（与 run store 同一原子写姿势）。
- 清除用 `Path.unlink(missing_ok=True)`；文件不存在不算错误。
- 新模块 `apps/code_review_pipeline/delivery/push_approval_registry.py` 提供
  `write_pending(...)` / `clear_pending(...)` / `list_pending(dir)` /
  `pending_path(dir, hitl_id)`。
- `hitl_id` 落盘前按 run store 同款白名单清洗（alnum 与 `-_.`），非法直接拒绝
  写入——侧车文件名不能成为路径注入点。
- 读不出来的 JSON：记 warning 并跳过，**不得**让巡检或 CLI 崩掉。

### R2 CLI 侧生命周期（`FeishuPushApprovalPort`）

- 发卡改用 `send_card_with_id`（拿 `message_id`）；sender 只有 `send_card`
  （返回 bool）时退回旧调用，此时 `message_id` 为空 → **不写侧车**（没有 id
  就没法改卡），流程行为与今天完全一致。
- 发卡成功且拿到 `message_id` → 写一条 pending 侧车；写失败只记 warning，
  **不得**影响审批（侧车是展示层）。
- 收到决定（approve / reject / bundle 不匹配）→ 先把卡改成对应终态
  （R4），再清侧车。改卡或清侧车失败只记 warning。
- 超时 → 先把卡改成 `expired` 终态，再清侧车，然后**照旧**抛
  `PushApprovalTimedOut`。超时语义、退出码、记录状态一律不变。
- 所有侧车/改卡异常都必须被吞掉并记录：审批结论只能由 Redis decision 决定，
  展示层失败不能把 approve 变成 reject、也不能把 timeout 变成成功。

### R3 巡检（merge worker）

新模块提供：

```python
async def sweep_push_cards(*, directory, sender, run_state_of, now, grace_s=120,
                           max_age_s=7*24*3600) -> int
```

- 只处理 `state == "pending"` 的侧车。
- 命中任一条件即改卡为 `expired` 并清侧车：
  1. `now >= expires_at + grace_s`（宽限 120s，避免与还活着的 CLI 抢改卡）；
  2. `run_state_of(run_id)` 返回的状态不是 `ready_for_push_approval`
     （交付已放弃 / 已推送 / 已终结）——这条让 abandon、合并、拒绝都能在
     一个 worker 周期内把卡收掉，不必等到 30 分钟超时。
- `run_state_of` 抛异常或返回 `None`（记录不存在 / 读失败）→ 不据此改卡，
  只按条件 1 判定。
- 改卡失败 → 保留侧车，下一轮再试；`now >= created_at + max_age_s`（7 天）
  → 无论改卡成败都丢弃侧车并记 warning（防止一个死 message id 永远重试）。
- `expires_at` / `created_at` 缺失或非数值 → 记 warning 丢弃该条。
- `card_message_id` 为空 → 丢弃（无法改卡）。
- `MergeWorker._run_once` 在 `_refresh_expired_approvals` 之后调用一次；
  任何异常只记 warning，**不得**中断轮询、心跳或摘要。
- 巡检不读 Redis、不依赖 HITL 登记是否还在：判据是侧车里的时间与 run 状态。

### R4 终态卡

`app/channels/feishu_cards.py` 新增三个 `hitl_kind`（无按钮，加入
`_NO_ACTION_KINDS`）：

| kind | 标题 | template | 文案 |
|---|---|---|---|
| `push_resolved_expired` | Agent Gateway - 推送审批已失效 | orange | `⏰ 审批已失效，未推送；需要继续请重新 delivery resume --run-id <run_id>` |
| `push_resolved_approved` | Agent Gateway - 推送已批准 | green | `✅ 已批准推送（审批人 <approver>）` |
| `push_resolved_rejected` | Agent Gateway - 推送被拒绝 | red | `🚫 已拒绝推送（审批人 <approver>）` |

改卡复用 `card_sender.update_card(message_id, card)`，卡片对象的
`channel` 必须是 `"feishu"`（`update_card` 的既有守卫）。

### R5 不改的东西

- 不改 `FEISHU_PUSH_APPROVAL_TIMEOUT_S`、HITL TTL、Redis key 结构。
- 不改 `PushApprovalPort.request` 的返回契约（`ChangeApproval` / `None` /
  `PushApprovalTimedOut`）。
- 不自动重发推送审批卡。
- 不改 `DeliveryRun` 模型与 `schema_version`。
- 不动合并审批卡的自愈（#149 已收口）。

## 2. 允许路径

```
apps/code_review_pipeline/delivery/push_approval_registry.py
apps/code_review_pipeline/delivery/feishu_approval.py
app/channels/feishu_cards.py
app/merge_worker.py
tests/unit/test_push_approval_registry.py
tests/unit/test_feishu_push_approval.py
tests/unit/test_feishu_cards.py
tests/unit/test_merge_worker.py
plans/push-approval-card-expiry-20261127.md
```

## 3. 验收标准

- **T1** `write_pending` / `list_pending` / `clear_pending` 往返正确；重复
  `clear_pending` 不抛；非法 `hitl_id`（含 `..`、`/`、空串）被拒绝；
  损坏 JSON 被跳过且不抛。
- **T2** 发卡拿到 message id → 侧车出现且字段齐全（run_id / repo / branch /
  card_message_id / created_at / expires_at / state=pending）。
- **T3** sender 只返回 bool（无 message id）→ 不写侧车，审批流程照常
  （approve 仍返回 `ChangeApproval`）。
- **T4** 批准 → 调用 `update_card` 一次且 kind 为 `push_resolved_approved`，
  侧车被清除，返回 `ChangeApproval` 字段不变。
- **T5** 拒绝 → `update_card` 为 `push_resolved_rejected`，返回 `None`。
- **T6** 超时 → `update_card` 为 `push_resolved_expired`，侧车被清除，
  仍抛 `PushApprovalTimedOut`。
- **T7** `update_card` 抛异常 / 返回 False → 审批结论不变（T4–T6 各自语义），
  异常不外泄。
- **T8** 巡检：`now >= expires_at + grace` → 改 `expired`、清侧车、返回 1；
  `now < expires_at + grace` → 不动。
- **T9** 巡检：run 状态不是 `ready_for_push_approval`（如 `abandoned`）→
  即使未到期也改卡并清侧车；`run_state_of` 抛异常 → 只按时间判定，不因异常改卡。
- **T10** 巡检：改卡失败保留侧车；超过 7 天丢弃；无 message id / 坏时间戳
  丢弃且不抛。
- **T11** `MergeWorker._run_once` 调用巡检一次；巡检抛异常不影响心跳、
  摘要与合并轮询（用替身断言后续步骤仍执行）。
- **T12** 三个新 kind 的卡片无按钮、文案与标题符合 R4；既有 kind 的渲染
  不变；`tests/unit` 全绿、`ruff check .` 全绿、无新增 skip/依赖。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_push_approval_registry.py tests/unit/test_feishu_push_approval.py tests/unit/test_feishu_cards.py tests/unit/test_merge_worker.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
V4  （人工，签收后）用 FEISHU_PUSH_APPROVAL_TIMEOUT_S=60 发一张卡并强杀 CLI，
    确认下一个 worker 周期内卡片变成「推送审批已失效」且侧车被清。
```

解释器一律 `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`。

## 5. 禁止

- 改 `DeliveryRun` schema / `schema_version`，或往运行记录里写新字段。
- 自动重发推送审批卡。
- 让展示层失败改变审批结论（approve / reject / timeout 三态）。
- 在巡检里读 Redis 或要求 HITL 登记存在才判定过期。
- 改超时、改 TTL、改 Redis key、动合并审批卡路径。
- 新增依赖；改动 Allowed paths 之外的文件。

## 6. 签收记录

（待所有者书面签收）
