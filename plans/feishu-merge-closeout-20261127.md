# 契约：飞书合并审批收口（#26 的最后两公里）

- **task id**：`feishu-merge-closeout-20261127`
- **分支**：`fix/feishu-merge-closeout-20261127`
- **背景**：issue #26 的六条验收里，五条已在代码里成立：`pull_request.opened`
  发通知卡（`send_pr_opened_card`）、CI 绿发带按钮的审批卡、回调过
  `approver_gate_error`、批准走 `MergeExecutor` + merge API（SHA 乐观锁）、
  `record_human_decision` + WAL 双审计、CI 红发诊断卡。

  剩下两处是**真的会让人点错**的缺口：

  1. **CI 转红后旧审批卡还带按钮。**`send_ci_failure_card` 只发一张新卡，
     之前那张 `merge_approval` 卡原样留在会话里。人点「批准」，回调走到
     `MergeExecutor.execute` → `transition(APPROVE)`，而记录已从
     `awaiting_approval` 变成 `ci_failed`，状态机抛
     `InvalidMergeTransition`，路由按 500 返回，聊天里得到
     「⚠️ 合并执行失败：...」。门禁没破（没有合并），但界面在骗人。
  2. **重复点击 / 已合并后再点。**第一下合并成功，记录进 `merged`（终态）；
     第二下同样走到 `transition` 抛错 → 500。人看到的是「失败」，实际早就成功了。

## 1. 语义

### R1 CI 红：把旧审批卡就地改成终态（而不是再发一张带按钮的旧卡）

`MergeNotifier.send_ci_failure_card(record, triage, pr_details)`：

- `record.card_message_id` 非空（说明发过审批卡）→ **就地** `update_card`
  为无按钮终态卡，`hitl_kind="merge_resolved_failed"`，正文
  `❌ 合并失败：CI 未通过（<失败 job 名，逗号分隔，最多 3 个>）`。
  失败 job 名取自 `triage.failing`（没有 triage 时退回 `record.ci_failing`，
  再没有就写 `CI 未通过`）。
- `update_card` 抛异常或返回 False → **降级**为现有行为：发一张新的诊断卡
  （`_build_ci_failure_card`），保证人至少知道 CI 红了。
- `record.card_message_id` 为空 → 现有行为不变（直接发诊断卡）。
- 返回值：改卡成功或新卡发出即 `True`；两者都失败为 `False`。
- **不得**同时改卡又发新卡（成功改卡时不发新卡）。

### R2 回调竞态：失效卡返回 409，不返回 500

`app/routes/feishu.py::_handle_merge_approval`：

- 新增捕获 `InvalidMergeTransition` 与 `MergeNotFound`：
  - 读当前记录（`merge_store.get(identity)`，identity 从 `hitl_id` 解析；
    `InvalidMergeTransition` 的 `state` 字段优先用于文案）；
  - 记录存在且已是 `merged` → 聊天回「✅ 该 PR 已合并」，卡片改成
    `merge_resolved_merged`；
  - 其它情况（`ci_failed` / `failed` / `rejected` / `superseded` /
    记录不存在）→ 聊天回「⚠️ 该审批卡已失效（当前状态：<state>）」，
    卡片改成 `merge_resolved_failed`，detail 写失效原因；
  - 返回 **HTTP 409**，JSON `{"status": "stale", "state": "<state>"}`。
- 改卡/发消息失败只告警，不改变 409 结论。
- 其它异常维持现有 500 路径不变。
- 不得因为 409 路径写 `record_human_decision`（那不是一次决策）；也不得
  对失效请求做任何 store 状态迁移。

### R3 不改的东西

- 不改 merge 状态机、`ALLOWED_ACTIONS`、SQL/schema。
- 不改审批卡按钮的 value、HITL 登记、`approver_gate_error`、`MergeExecutor`
  的合并语义。
- 不改 PR opened 通知卡、每日摘要、推送审批（A1）。
- 不给 `merge_resolved_failed` 增加按钮。

## 2. 允许路径

```
apps/code_review_pipeline/notifications/merge_notifier.py
app/routes/feishu.py
tests/unit/test_merge_notifier.py
tests/unit/test_feishu_route.py
plans/feishu-merge-closeout-20261127.md
```

## 3. 验收标准

- **T1** `card_message_id` 非空 + triage 有 2 个失败 job → `update_card`
  调用一次，kind 为 `merge_resolved_failed`，正文含两个 job 名；
  `send_card` **未被调用**；返回 True。
- **T2** `card_message_id` 非空 + `update_card` 返回 False → 退回
  `send_card`，新诊断卡发出，返回 True。
- **T3** `card_message_id` 非空 + `update_card` 抛异常 → 同上退回新卡，
  异常不外泄。
- **T4** `card_message_id` 为空 → `update_card` 不被调用，`send_card`
  发出，返回 True（既有行为）。
- **T5** 无失败 job 信息（triage 与 `ci_failing` 都空）→ 正文退化为
  `❌ 合并失败：CI 未通过`，仍走改卡路径。
- **T6** 回调时记录已 `merged` → HTTP 409、`{"status":"stale"}`、
  聊天收到「已合并」、卡片改成 `merge_resolved_merged`、**没有**任何
  store transition。
- **T7** 回调时记录为 `ci_failed` → HTTP 409、state 出现在响应与聊天文案里、
  卡片改成 `merge_resolved_failed`、没有 transition。
- **T8** 记录不存在（`MergeNotFound`）→ HTTP 409，聊天提示审批卡失效，
  不抛 500。
- **T9** 其它异常（例如 `get_pr` 网络错误导致的执行失败）仍返回 500，
  既有文案不变。
- **T10** 既有 `tests/unit` 全绿、`ruff check .` 全绿、无新增 skip/依赖、
  未删弱化断言。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_merge_notifier.py tests/unit/test_feishu_route.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

解释器一律 `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`。

## 5. 禁止

- 状态机里新增迁移（此处只读状态，不改状态）。
- 失效回调写人工决策审计。
- 同时改卡又发新卡；给终态卡加按钮。
- 新增依赖；改 SQL/schema；动 `MergeExecutor` 的合并语义。
- 改动 Allowed paths 之外的文件。

## 6. 签收记录

（待所有者书面签收）
