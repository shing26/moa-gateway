# Contract — A2 终端通道身份语义收尾（Approver ≠ Operator，fail-closed 写审计）

**冻结于实现之前**（2026-11-18）。路线图阶段 1 的收尾件：PR #106 交付了**合并审批**
通道的身份校验（GitHub 评论、白名单、approver ≠ PR 作者、fail-closed）；本契约把
同一套身份语义补到**终端通道**，并让人工决策的审计行携带身份标记。

## 为什么

`docs/roadmap-generality-20261117.md` A2 交付判据原文：

> 审批人必须是可验证身份（GitHub login / Feishu open_id），必须属于白名单，
> 必须 ≠ 推送者；不满足时 fail-closed 并写进审计记录。
> 终端通道要么接入同一套校验，要么在审计里显式标记为「未验证身份」。

现状事实（main `6b5fc1a` 亲自核对）：

1. `app/cli.py` `TerminalPushApprovalPort`：approver 是**自由输入的字符串**，不校验
   白名单、不校验是否等于推送者（token 主人）、审计里没有任何身份字段；
2. `apps/code_review_pipeline/delivery/flow.py` 推送审批通过后 `_move(PUSHING,
   "approved")` 的审计 detail 只有 `{"action": "approved"}`——**谁批的推送在交付审计里查不到**；
3. `task_audit.record_human_decision` 的 detail 不带通道与身份标记，终端
   `cmd approve` 与 GitHub 评论通道写出的行**无法区分**「已验证身份」与「自称的名字」。

## 设计

### 1. 终端推送审批闸门（`app/cli.py` `TerminalPushApprovalPort`）

操作者身份 = `GITHUB_TOKEN` 的登录名，用既有
`GitHubClient.get_authenticated_login()`（`routing/github_client.py:257`）读取。
判定矩阵（大小写不敏感比较）：

| 场景 | `GITHUB_APPROVER_LOGINS` 非空 | 空名单 + `GATEWAY_ALLOW_INSECURE=1` | 空名单 + 无 insecure |
|---|---|---|---|
| approver ∈ 白名单 **且** ≠ token 主人 | **批准** | — | — |
| approver == token 主人 | **拒绝**（`self_approval`） | 批准，标记 `gate=insecure_self_approval` | **拒绝**（`self_approval`） |
| approver ∉ 白名单 / 为空 | **拒绝**（`not_allowlisted`） | 批准，标记 `gate=insecure` | **拒绝**（`not_allowlisted`） |

- 白名单语义与 `app/middleware/auth.py:approver_gate_error` 同一三态：
  配了白名单只放行白名单成员；没配白名单只在显式 insecure 时放行；否则拒绝。
  **空白名单 + 无 insecure = 拒绝**（不能回到今天的门全开）。
- `get_authenticated_login()` 失败（网络/凭证）：**抛异常**，让 `DeliveryFlow`
  现有的 except 路径把它映射为 `NEEDS_HUMAN` / `INFRA_ERROR`——这是身份读取
  的 infra 故障，不是「人拒绝了」，不得吞成 `None`（那会进 `REJECTED`）。
- 非 TTY / EOF 的现有拒绝路径**原样保留**（`None` → `REJECTED`）。
- 拒绝时向终端打印稳定原因码（`self_approval` / `not_allowlisted`）。
- 端口构造时接收 github client（`cmd_delivery_run` 里传 `build_github_client()`
  的实例；测试可注入桩）。

### 2. `ChangeApproval` 身份元数据（`change/pipeline.py`）

新增**带默认值**的四个字段（不破坏既有构造点与位置兼容）：

```
channel: str = "terminal"
identity_verified: bool = False
operator_login: str = ""      # 已验证的推送者身份（token 主人），读不到为空
gate: str = ""                # "allowlist" | "insecure" | "insecure_self_approval"
```

`approver` 语义不变：终端里仍是自称的名字（`identity_verified=False` 即如实
标记「这是自称，未验证」）。

### 3. 交付审计补全（`delivery/flow.py`）

推送审批通过的 `_move(run, PUSHING, "approved", extra=...)` 必须带上：
`approver`、`channel`、`identity_verified`、`operator_login`、`gate`。
**自此之后，交付审计里每一条「谁批准了推送」都有身份字段**——哪怕是
`identity_verified=False`，也比查不到强。

### 4. 人工决策审计带身份标记（`task_audit.py` + 两个调用方）

- `record_human_decision` 新增可选参数 `identity_verified: bool = False`、
  `channel: str = ""`，写进 detail dict；
- 合并审批评论通道（`merge_comment_approval.py` 的 recorder 调用）传
  `channel="github_comment"`、`identity_verified=True`——评论作者来自 GitHub API
  `user.login`，是已验证身份；
- 终端 `cmd approve`（review 审批）传 `channel="terminal"`、`identity_verified=False`；
- 其余调用方不动（默认 `False` = 「未做声明」，诚实默认）。

## Allowed paths

```
app/cli.py                                          (TerminalPushApprovalPort 闸门 + cmd approve 传身份标记)
app/config.py                                       (如需暴露 GITHUB_APPROVER_LOGINS 的 settings 字段；仅此)
apps/code_review_pipeline/change/pipeline.py        (ChangeApproval 四个默认字段)
apps/code_review_pipeline/delivery/flow.py          (approved 审计 detail 补身份字段)
apps/code_review_pipeline/task_audit.py             (record_human_decision 新增两个可选参数)
apps/code_review_pipeline/merge_comment_approval.py (recorder 调用传 channel/identity_verified)
tests/unit/test_terminal_push_approval.py           (新文件)
tests/unit/ 其他新增/调整的测试文件                   (flow / audit / merge poller 的既有文件内新增)
plans/a2-terminal-identity-20261118.md              (本文件)
docs/roadmap-generality-20261117.md                 (只更新 A2 一行的状态)
```

## Acceptance criteria

1. **判定矩阵逐格有测试**：上表每一格至少一个用例（含大小写变体，如
   approver `Alice` vs 白名单 `alice`、token 主人 `ALICE` vs approver `alice`）。
2. **自审批拒绝**：白名单模式下 token 主人（即使在白名单里）批准自己的推送 → 拒绝；
   insecure 模式 → 放行但 `gate="insecure_self_approval"`。
3. **fail-closed 边界**：空白名单且无 insecure → 拒绝；`get_authenticated_login()`
   抛异常 → 异常上抛（由 flow 映射 `INFRA_ERROR`），不得静默放行也不得伪装成拒绝。
4. **非 TTY 语义不变**：非交互 stdin / EOF 仍是 `None` → `REJECTED`，原因打印不变。
5. **审计可查**：flow 的 `approved` 审计 detail 含五项身份字段；构造 delivery flow
   级测试（打桩端口）断言字段落进 audit record。
6. **人工决策审计带标记**：`record_human_decision` 新参数进 detail；合并评论通道
   `identity_verified=True`；终端 `cmd approve` `identity_verified=False`；既有调用方
   不传参时 detail 多出的两个键为默认值且行为不变（回归测试）。
7. **既有测试全绿**，`tests/` 只新增或加用例；`merge_executor`、`webhook`、`feishu`
   路由的既有语义零改动。
8. **证据绑定**：HEAD SHA + `git status --short`。

## Verification commands

```
V1  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit/test_terminal_push_approval.py -q
V2  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit/test_task_audit.py tests/unit/test_merge_comment_approval.py tests/unit/test_delivery_flow.py -q   # 文件名以实现方核对为准
V3  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m ruff check .
V4  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit -q
```

`V2` 文件名以实现方核对为准，PR 里写清；找不到对应文件就只跑实际存在的并在 PR 说明。

## Forbidden

- 改 `approver_gate_error` / `insecure_mode_enabled` 的既有三态语义。
- 弱化合并审批通道的任何校验（作者只认 API、approver ≠ PR 作者等）。
- 在日志或审计里新增记录审批交互正文（终端输入内容不得进日志）。
- 把 `get_authenticated_login()` 失败吞成批准或普通拒绝。
- 改 `ChangeApproval` 既有字段的位置顺序或必填性。
- 在文档里宣称「自审批已不可能」——终端自称身份永远不可验证，insecure 逃生口
  仍在；文档只能说「未验证身份有审计标记、自审批默认被拒」。

---

## 修订 1（2026-11-19，实现方声明偏差的裁决）

实现完成于 `c4988fc`，声明四处偏差，裁决如下：

1. **分支基于本地 `origin/main` = `6b5fc1a`**（上游 OOM 两次 fetch 失败）。
   裁决：接受——协调方已独立 fetch 确认 remote main 仍为 `6b5fc1a`，无遗漏提交。
2. **契约设计第 4 条「终端 `cmd approve` 传 `channel="terminal"`」未落地**：该调用
   经 `approval.py::approve_task`（不在 Allowed paths，且与飞书共用），硬编码会把
   飞书审批误标为终端。裁决：接受——正确性的理由成立（误标比缺省更糟）；判据 6
   的可测形态（终端推送审批标记完整落地、`cmd approve` 行为 `identity_verified=False`
   诚实默认）已满足。
3. **`eval/gate_state.py` 越出 Allowed paths**：两个 L2 gate 探针无参构造
   `TerminalPushApprovalPort()`，端口加参后必然 TypeError；修复为注入拒绝路径上
   不可触达的身份桩。裁决：有条件接受——属签名变更的机械后果；**verifier 必须亲自
   确认**该 diff 仅限构造点注入、探针判定逻辑与断言零改动。
4. **`change/pipeline.py::_record_approval` 不转发身份字段到 `record_human_decision`**：
   契约明文「其余调用方不动」，且第 3 条要求的形态（flow 审计 detail）已完整落地。
   裁决：接受。
