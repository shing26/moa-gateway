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

**所有者书面签收**：用户消息「签收 #141」（2026-11-25）。

**合并**：PR #141 → `main`，merge commit
`1efb1521741c746123be71dc2bea7896e3fb19d1`（HEAD `65b822aa1d43`）。

**验收证据**：独立 Verifier 最终 `ACCEPT`（HEAD `141535d`），F1–F6 全 PASS，
P0/P1 无；首轮 `ACCEPT_WITH_NITS` 的三个可修 nit（Unicode 数字解析抛
`ValueError`、空 card id 用 INFO、`error` 分支未覆盖）已收掉并复核，
Unicode 复现现在只告警返回 `None`。V1 `63 passed`；V2
`2381 passed, 6 skipped`；V3 `All checks passed!`。GitHub CI 实查
（`65b822a`）：`test` success、`integration` success、`docker` skipped。

**端到端观察（边界如实记录）**：#141 是「审批决定点改卡」上线后的第一张
真实审批卡。19:16 发出「等待审批」卡（`om_x100b7f636caf60a4dfd38dbc2f9cb41`），
批准与合并在 19:23 发生，同刻该卡被标记为已更新（`updated=true`，
`update_time=19:23`），通道内出现「✅ 已批准，PR 已合并」+ merge sha
`1efb152`。消息接口只回发卡时的原始 content，卡面逐字未复核，故本条按
「更新时刻与决定同刻」记录，不作为逐字卡面证据。

**运行时复核（2026-11-25 02:05，未通过）**：`GET 127.0.0.1:8083/healthz`
返回 `degraded`——`redis=fallback_memory`、`vectordb=degraded
(PoolTimeout)`、`audit_chain=ok`、`merge_worker=degraded: 心跳缺失`。
进程表里仍有两个 `python -m app.merge_worker`（两个 venv 各一份），但
心跳缺失，说明「worker 已重启并生效」当前无法确认。故本条不写正面运行时
确认，记为待复核。

**残余（已知并接受）**：`rejected` 终态复用 `notification` kind（标题
「通知」/蓝色），不是拒绝专用红色终态卡；契约只约束文案与无按钮。真实
「拒绝」路径仍只有单测覆盖，无线上样本。

## 7. 修订 1：端到端改卡证据（2026-11-25 03:43）

上文「运行时复核（02:05，未通过）」的后续：根因不在本单代码，而是
Docker Desktop 自动更新失败导致 Postgres/Redis 容器停机，网关退化为内存
Redis、`vectordb` 连接池超时，worker 心跳也随之消失。容器恢复、两个进程
重启到 `1efb152` 后复测通过。

**生产路径证据链（PR #142，真实飞书审批）**：

| 环节 | 证据 |
|---|---|
| 记录创建 | 网关停机期间 #142 的 `opened` webhook 返回 500；用「关闭→重开」补触发 `reopened`，记录落 `watching` |
| 审批卡 | worker 发卡后，Postgres 中 `state=awaiting_approval`、`card_message_id=om_x100b7f6a3ae518a4`（**T2 的单列写回在生产库真实验证**） |
| 批准 + 合并 | 03:43:02 `PUT /pulls/142/merge` 200；`merging -> merged (complete)`；merge sha `fce63a1bb67c02599197c3f2177bc4f0bc2ecf7a` |
| 就地改卡 | 03:43:03.999 `moa.merge_notifier resolved card updated for ... (merged)`（`update_card` 返回 ok） |
| 落库终态 | `state=merged`、`approver=ou_ae3e5cc009fae3462147e15e8c271602`、`merged_sha=fce63a1bb67c`、`card_message_id` 仍为该卡 |

这条路径正是 #139 T3 缺失的那一段：旧实现在 PR `closed` webhook 里找
`list_open()`，通道内合并时记录已终态，永远查不到；现在改卡挂在
`_handle_merge_approval` 的决定点，`terminated=0` 不再是漏改卡的理由。

**未覆盖**：`rejected` 与 `superseded` 仍只有单测，无线上样本。

## 8. 新发现的缺口（不属本单，待立契）

`PostgresMergeStore` 只在 `self._conn is None` 时建连，容器重启或网络中断
后不会重连。实测：Postgres 容器重启后 worker 每轮抛
`OperationalError: the connection is closed`，空转直到进程重启。本次靠重启
进程绕过，未在代码层修复。
