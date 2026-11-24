# 契约：飞书审批决定后的终态改卡

- **task id**：`merge-card-resolve-on-decision-20261124`
- **分支**：`fix/merge-card-resolve-on-decision-20261124`
- **背景**：#139 上线后第一次真实样本（PR #140）暴露了 T3 的路径缺口。

  `#140` 的 CI 变绿后收到审批卡，人在飞书点「批准」→ `MergeExecutor` 合并
  成功（`2d819de`）→ 记录在通道内变为 `merged`。随后 GitHub 的 PR
  `closed` webhook 到达，`app/routes/github_delivery.py:_on_closed` 只查
  `merge_store.list_open()`，此时该 PR 已经没有 open 记录，于是
  `terminated=0`，`send_resolved_card` 从未被调用——**审批卡永远停在
  「等待审批」**。

  #139 的 T3 只覆盖了两条路径：通道外合并/关闭（记录仍是 open）与
  supersede。最常用的「飞书批准 → 通道内合并」反而没有触发点。

## 1. 语义

### F1 审批回调是决定性的触发点

`app/routes/feishu.py:_handle_merge_approval` 在
`executor.execute(hitl_id, action, operator_id)` 返回后，用 `hitl_id`
解析出 `MergeRecord.identity`，从 `merge_store.get(identity)` 取**最新**记录，
按 outcome 调用 `MergeNotifier.send_resolved_card`：

| outcome | 改卡参数 |
|---|---|
| `outcome.merged` | `("merged", detail=outcome.sha)` |
| `outcome.status == "rejected"` | `("rejected", detail=operator_id)` |
| 其他（`error` / `not_mergeable`） | `("failed", detail=outcome.message)` |

- 记录不存在、`card_message_id` 为空、`update_card` 抛异常 → 只告警，
  **不得**改变审批响应、不得 500、不得回滚合并结论。
- 改卡必须在 `executor.execute` 之后、返回 JSONResponse 之前完成（顺序可读性
  优先，失败不影响结果）。

### F2 新增 `rejected` 终态文案

`send_resolved_card` 的 outcome 增加 `rejected`：

```
🚫 已拒绝（审批人 <approver>）
```

其余三种（`merged` / `superseded` / `failed`）文案与无按钮行为保持 #139 的
实现不变。

### F3 `hitl_id` 解析

`hitl_id` 与 `MergeRecord.task_key` 同构：`owner/repo#123@<full_sha>`。
解析必须只按最后一个 `@` 与最后一个 `#` 切分；解析失败时只告警，跳过改卡，
不抛异常。不得为此修改身份三元组或 `task_key` 格式。

## 2. 允许路径

```
app/routes/feishu.py
apps/code_review_pipeline/notifications/merge_notifier.py
tests/unit/test_feishu_route.py
tests/unit/test_merge_notifier.py
plans/merge-card-resolve-on-decision-20261124.md
```

## 3. 验收标准

- **F1** 审批成功路径：`send_resolved_card` 恰好调用一次，参数为
  `("merged", detail=<merge sha>)`；卡片无按钮；审批 JSONResponse 不变。
- **F2** 拒绝路径：参数为 `("rejected", detail=<operator_id>)`，文案含
  `🚫 已拒绝`；`MergeExecutor` 的拒绝语义与状态转移不变。
- **F3** 失败路径：`error` / `not_mergeable` 参数为
  `("failed", detail=<message>)`，文案含 `❌`。
- **F4** 记录不存在 / `card_message_id` 为空 / `send_resolved_card` 抛异常：
  不抛 500、审批响应正常返回、只告警。
- **F5** 不改变审批身份校验、`bundle_hash` 绑定、head-SHA 乐观锁、
  合并/拒绝结论或状态机转移表。
- **F6** 不新增依赖；不删弱化断言；`ruff check .` 全绿。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_feishu_route.py tests/unit/test_merge_notifier.py tests/unit/test_merge_executor.py tests/unit/test_merge_store.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

解释器一律 `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`。

## 5. 禁止

- 只在 `_on_closed` 里补救（通道内合并时记录已终态，永远查不到）。
- 因改卡失败而改变审批结论、回滚合并或返回 5xx。
- 改 `merge_state.py` 的转移表，或改 `task_key` / identity 格式。
- 在 `card_message_id` 为空时另发一条新消息。
- 新增依赖；改动 Allowed paths 之外的文件。

## 6. 签收记录

（待所有者书面签收）
