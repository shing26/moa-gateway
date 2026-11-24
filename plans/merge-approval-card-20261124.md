# 契约：合并审批卡的信息补全与终态改卡

- **task id**：`merge-approval-card-20261124`
- **分支**：`feat/merge-approval-card-20261124`
- **背景**：2026-11-24 的 #136 合并审批卡只有三行——
  `PR / SHA / CI: success`。人实际是在签一个 40 位哈希：看不到标题、改了多少行、
  谁写的、点了会发生什么。同一张卡在 PR 已经合并后仍显示「等待人工审批」，
  因为它从不回写自己（`MergeRecord.card_message_id` 字段存在但从未被赋值）。

  门禁的**执行**是真的（SHA 绑定 + 标状态机 + 审计），缺的是**知情**。

## 1. 语义

### T1 审批卡内容补全

发审批卡前，由 `MergeWorker` 用现有 `github.get_pr(repo, pr_number)` 取：
`title` / `additions` / `deletions` / `changed_files` / `base.ref` /
`html_url` / `user.login`。卡片正文改为：

```
标题: <title>
变更: <changed_files> files  +<additions> −<deletions>  → <base>
作者: <login>
PR  : #<number> <html_url>
SHA : <head_sha 前 12 位>
CI  : <ci_state>
批准后: 以 merge commit 合并到 <base>，并写入审计（审批人 + 时间）
```

- `get_pr` 失败/字段缺失 → **降级为现有三行**（`PR / SHA / CI`）并发卡，
  不得因为富化失败而漏发审批（fail-open 仅限展示层）。
- 不改变 HITL 登记、按钮 value、`bundle_hash` 绑定与合并语义。

### T2 记录卡片消息 id

`FeishuCardSender` 新增 `send_card_with_id(card) -> str`（失败返回 `""`）；
`send_card` 保持 `bool` 语义不变（内部复用它）。
`MergeNotifier.send_approval_card` 返回 `(sent: bool, message_id: str)` 或
等价的显式结构；`MergeWorker` 在成功后把 `message_id` 写回
`MergeRecord.card_message_id`，失败只告警。

写回必须走 store 的新方法 `set_card_message_id(identity, message_id) -> None`
（内存与 Postgres 两个实现都要有），**不得**用
`store.save(replace(record, ...))`：`PostgresMergeStore.save()` 的
`ON CONFLICT` 只更新 `updated_at`（`merge_store.py:249-264`），业务列一律不动，
内存 store 的测试会绿而生产永远不会落库。也不得为此扩大 `save()` 的
`DO UPDATE` 白名单。

### T3 终态改卡

新增 `MergeNotifier.send_resolved_card(record, outcome, detail)`：

- `record.card_message_id` 为空 → 不发新消息（避免二次打扰）；
- 有 id → `update_card` 就地替换为无按钮的终态卡：
  - `merged`：`✅ 已合并 <merged_sha 前 12 位>（审批人 <approver>）`
  - `superseded`：`⚠️ 本卡已作废：PR head 变了（<旧 sha 前 12 位>）；新记录已重建`
  - `failed`：`❌ 合并失败：<failure_reason 摘要>`
- `update_card` 失败只告警，不抛异常、不阻塞 webhook。

触发点放在已有终态落库的路径：`app/routes/github_delivery.py` 的 PR closed
（merged/failed 分支）与 supersede 分支。

## 2. 允许路径

```
app/channels/feishu_cards.py
app/merge_worker.py
app/routes/github_delivery.py
apps/code_review_pipeline/merge_store.py
apps/code_review_pipeline/notifications/merge_notifier.py
apps/code_review_pipeline/merge_poller.py
tests/unit/test_feishu_cards.py
tests/unit/test_merge_notifier.py
tests/unit/test_merge_store.py
tests/unit/test_merge_worker.py
tests/unit/test_github_delivery_webhook.py
plans/merge-approval-card-20261124.md
```

## 3. 验收标准

- **T1** 富化成功时卡片含标题、`+A −D`、目标分支、作者、`/pull/` 链接、短 SHA
  与「批准后」说明；`get_pr` 抛异常时仍发卡且正文是旧三行；
  卡片仍是 `merge_approval` 类型且带批准/拒绝按钮。
- **T2** 发送成功返回 message id 并通过 `set_card_message_id` 写回记录；
  发送失败时记录不变；`send_card` 的既有 bool 契约不变（既有调用方测试不改）。
  Postgres 实现的 `set_card_message_id` 只更新 `card_message_id` 一列
  （用 fake psycopg 捕获 SQL 断言），且 `save()` 的冲突分支仍只写
  `updated_at`。
- **T3** 三种终态各更新一次卡片且无按钮；`card_message_id` 为空时不发任何消息；
  `update_card` 抛异常只告警；不改变合并/作废的状态机转移。
- **T4** 不新增依赖；不删弱化断言；`ruff check .` 全绿；
  既有审批链路测试（poller/executor/state）全绿。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_feishu_cards.py tests/unit/test_merge_notifier.py tests/unit/test_merge_store.py tests/unit/test_merge_worker.py tests/unit/test_github_delivery_webhook.py tests/unit/test_merge_poller.py tests/unit/test_merge_executor.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

解释器一律 `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`。

## 5. 禁止

- 因富化/改卡失败而漏发审批卡或改变合并结论（展示层失败必须降级）。
- 修改审批身份校验、`bundle_hash` 绑定、head-SHA 乐观锁或状态机转移表。
- 用 `update_card` 发送新的审批卡（它只能改已有消息）。
- 用 `save()` 回写 `card_message_id`，或扩大 `save()` 的 `DO UPDATE` 白名单。
- 新增依赖；改动 Allowed paths 之外的文件。

## 6. 签收记录

（待所有者书面签收）
