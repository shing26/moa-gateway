# Contract — 合并审批的 GitHub 原生通道（A1 + A2 第一切片）

**冻结于实现之前**（2026-11-17）。方向由所有者确认：「远程审批走 GitHub 原生通道：
白名单 login 在 PR 上评论 `APPROVE <bundle_hash>`，免公网地址」。

## 为什么要这么做

现状实测（`app/cli.py:340`、`app/config.py:114`、`app/middleware/auth.py:36`）：

- 推送审批只认终端里**自由输入**的 `approver` 名字，不校验白名单、不校验是否等于推送者；
- `HITL_APPROVER_IDS` 白名单只在飞书/webhook 回调里生效，而那条路要求公网地址
  （funnel → `127.0.0.1:8083`，当前无监听），且与 nexus-vibe 的地址共用未了；
- 于是「第二个人在任何地方都能批准」这件事，既缺渠道、又缺可验证身份。

GitHub 原生通道把两件事一起解决：**PR 评论是现成的、带认证作者身份的外部动作**，
不需要入站地址（主动轮询即可），也不和任何其他服务抢端口。

## 范围：只做合并审批

链路里有两道人工闸门：

1. **推送审批**（`PushApprovalPort`，在分支推送**之前**）——此刻还没有 PR，
   评论无从谈起。本切片**不动**它，仍走终端；这是已知限制，写进 ADR 的「未做」。
2. **合并审批**（`MergeState.AWAITING_APPROVAL` → `MergeExecutor`）——PR 已存在，
   `MergeRecord.bundle_hash` 已落库（`delivery/adapters/github.py:166` 写入）。
   **本切片只做这一道。**

## 身份模型（这就是 A2 的最小实现）

一条评论算数，当且仅当**同时**满足：

1. **作者身份来自 API**：用 `comment["user"]["login"]`，绝不从评论正文里解析身份；
2. **在白名单里**：`GITHUB_APPROVER_LOGINS`（逗号分隔，去空白，大小写不敏感）。
   白名单为空 → 通道视为未配置，**一条都不批**（fail-closed）；
3. **不是 PR 作者**：`get_pr().user.login` 与评论作者不同（大小写不敏感）。
   拿不到 PR 作者（API 失败）→ 不批；
4. **正文精确匹配**：`strip()` 后必须**恰好**等于 `APPROVE <bundle_hash>`，
   哈希取该 `MergeRecord.bundle_hash`，大小写敏感。多一个字、少一个空格都不算；
5. **绑定到具体 bundle**：哈希必须与记录上的 `bundle_hash` 一致；记录上为空字符串时
   → 不批（宁可不批，也不能拿「任意哈希」当通行证）。

`HITL_APPROVER_IDS` 与 `GITHUB_APPROVER_LOGINS` 是**两套身份命名空间**，不得互相回退：
飞书白名单里的人不会因为同名而被 GitHub 通道接受。

## 行为

- 轮询发生在 `app/merge_worker.py` 的既有循环里（`_run_once` 之后），只处理
  `state == AWAITING_APPROVAL` 的记录，每轮每个记录最多执行一次批准。
- 命中后的执行路径与 `app/routes/webhook.py:_handle_merge_approval` 同构：
  `MergeExecutor.execute(...)` + `record_human_decision(operator=f"github:{login}", ...)`，
  审计里带 `bundle_hash`、`run_id`、`trace_id`。
- **幂等**：记录离开 `AWAITING_APPROVAL` 后不再重复执行；同一评论重复轮询不会二次合并。
- **fail-closed**：GitHub API 任何失败（PR 读不到、评论读不到、限流）→ 记日志、跳过；
  **永不**因为「查不到所以放行」。
- 通道开关 `GITHUB_COMMENT_APPROVAL_ENABLED` 默认 `False`；未开时 worker 完全不调用该路径。

## Allowed paths

```
app/config.py                                    (只加两个设置项，且只读环境变量)
app/merge_worker.py                              (只加轮询调用与构造依赖，不改既有 CI 轮询语义)
apps/code_review_pipeline/routing/github_client.py    (只加 list_issue_comments 一个方法)
apps/code_review_pipeline/merge_comment_approval.py   (新文件：判定 + 轮询器)
tests/unit/**                                    (只新增)
docs/adr/adr-040-github-comment-approval.md      (新 ADR，记录身份模型与未做的推送闸门)
plans/github-approval-20261117.md                (本文件)
```

## Acceptance criteria

1. **判定纯函数可单测**：`comment_approval(record, comments, pr, allowlist)` 之类不依赖网络，
   输入评论列表 + PR 元数据，输出「批 / 不批 + 原因」，每条拒绝原因是一个稳定字符串
   （`not_allowlisted` / `is_pr_author` / `hash_mismatch` / `body_mismatch` / `empty_bundle_hash`）。
2. **五条身份规则各有一个反例测试**，且反例是「同一条评论只破坏一个条件」的对照实验：
   允许名单内非作者通过；作者自己评论被拒；名单外人被拒；哈希错被拒；正文多一个词被拒。
   登录名大小写不同仍通过（正面用例）。
3. **fail-closed**：评论 API 抛异常、PR 元数据抛异常、白名单为空、`bundle_hash` 为空——
   四种情形各一条测试，断言「不执行合并」且不抛异常打断 worker 循环。
4. **执行路径同构**：命中后调用 `MergeExecutor.execute`，`operator` 形如 `github:<login>`，
   并写 `record_human_decision`；用假 executor/假审计断言参数，不真合并。
5. **幂等**：对同一记录连续轮询两次，第二次不产生第二次执行（用假 executor 计数）。
6. **开关默认关**：`GITHUB_COMMENT_APPROVAL_ENABLED` 未设置时 worker 一轮下来不调用评论 API
   （假 github 记录调用次数）。
7. **不碰既有闸门**：飞书/webhook 合并审批、推送审批、状态机转移表、`HITL_APPROVER_IDS`
   语义均不变；`app/routes/**` 零改动。
8. **既有测试全绿**，`tests/` 只新增不删改。
9. **证据绑定**：HEAD SHA + `git status --short`。

## Verification commands

worktree 无 `.venv`：

```
V1  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit/test_merge_comment_approval.py -q
V2  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit/test_merge_worker.py tests/unit/test_merge_poller.py tests/unit/test_github_client_errors.py tests/unit/test_config.py -q
V3  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m ruff check .
V4  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit -q
```

V1 的测试文件名以实际创建为准，需在 PR 里写清楚。

## Forbidden

- 从评论正文里解析身份（只认 API 的 `user.login`）。
- 允许「作者自批」，或把 `HITL_APPROVER_IDS` 当作 GitHub 白名单的回退。
- 用「读不到就是没问题」的放行：任何异常都必须落到「不批」。
- 让 `APPROVE` 不带哈希、或哈希不匹配 bundle 也通过。
- 改合并状态机、改飞书/webhook 路由、把推送审批也接到评论上。
- 在日志或审计里写入评论正文或 token。
- 为通过测试而放宽判定（反例测试必须真实覆盖五条规则）。
