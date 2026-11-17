# ADR-040: 合并审批的 GitHub 原生通道——PR 评论即可验证的第二人批准

日期：2026-11-17
状态：已实施（第一切片：只做合并审批）
关联：ADR-021（合并通道与状态机）、ADR-030（受治理变更）、ADR-031（受治理仓库交付）

## 背景

合并审批此前只有两条路，各自缺一半：

1. **飞书卡片回调**：有 `HITL_APPROVER_IDS` 白名单，但回调要求公网可达地址
   （funnel → `127.0.0.1:8083`，当前无监听），且与另一个服务的地址共用未了；
2. **终端 / HTTP 回调**：`app/cli.py` 让审批人自由输入名字，既不校验白名单，
   也不校验是否等于推送者。于是"第二个人在任何地方都能批准"这件事，既缺渠道、
   又缺可验证身份。

PR 评论把两件事一起解决：它是**现成的、带认证作者身份的外部动作**，不需要入站
地址（主动轮询即可），也不和任何其他服务抢端口。白名单 login 在 PR 上评论
`APPROVE <bundle_hash>`，就是一次可审计的远程批准。

## 范围

链路里有两道人工闸门：

1. **推送审批**（分支推送**之前**）：此刻还没有 PR，评论无从谈起。**本切片不动**，
   仍走终端；
2. **合并审批**（`MergeState.AWAITING_APPROVAL` → `MergeExecutor`）：PR 已存在，
   `MergeRecord.bundle_hash` 已落库。**本切片只做这一道。**

## 决策

### 1. 身份模型五条（缺一不可）

一条评论算数，当且仅当**同时**满足：

1. **身份只来自 API**：用 `comment["user"]["login"]`。**绝不**从正文解析身份
   （正文里写"我是 alice"没有任何作用）；
2. **在白名单里**：`GITHUB_APPROVER_LOGINS`（逗号分隔，去空白，大小写不敏感）。
   白名单为空 → 通道视为未配置，**一条都不批**；
3. **不是 PR 作者**：`get_pr().user.login` 与评论作者不同（大小写不敏感）。
   拿不到 PR 作者 → 不批（规则本身 fail-closed）；
4. **正文精确匹配**：`strip()` 后**恰好**等于 `APPROVE <bundle_hash>`，大小写
   敏感。多一个词、少一个空格都不算；
5. **绑定到具体 bundle**：哈希必须等于该 `MergeRecord.bundle_hash`；记录上为空
   字符串时 → 不批。宁可漏批，也不能拿"任意哈希"当通行证。

拒绝原因固定为五个稳定字符串：`not_allowlisted`、`is_pr_author`、
`hash_mismatch`、`body_mismatch`、`empty_bundle_hash`。其中"读不到 PR 作者"
复用 `is_pr_author`：它同样表示"无法证明评论者不是作者"。

### 2. 判定与执行分离

- `apps/code_review_pipeline/merge_comment_approval.py` 的 `comment_approval`
  是纯函数：输入记录、评论列表、PR 元数据、白名单，输出「批 / 不批 + 原因码」。
  不联网、不写库，五条规则可逐条单测；
- `MergeCommentApprovalPoller` 只负责取数、判定、命中后执行。执行路径与
  `app/routes/webhook.py:_handle_merge_approval` 同构：
  `MergeExecutor.execute(..., "approve", operator)` +
  `record_human_decision(operator=f"github:{login}", decision="approve",
  bundle_hash=..., run_id=..., trace_id=...)`。

### 3. 轮询与幂等

- 挂在既有合并 worker（`app/merge_worker.py`）的每轮 CI 轮询之后，只处理
  `state == AWAITING_APPROVAL` 的记录；
- 命中后执行前**重新读记录状态**，不用本轮开头的快照；
- 记录一旦离开 `AWAITING_APPROVAL`（`merging` / 终态）即不再命中；同一评论
  重复轮询不会二次合并。

### 4. fail-closed

GitHub API 任何失败（评论读不到、PR 读不到、限流）→ 记日志、跳过该记录，继续
处理下一条，**永不**打断 worker 循环，也永不因为"查不到所以放行"。

### 5. 开关与凭据

- `GITHUB_COMMENT_APPROVAL_ENABLED` 默认 `False`；未开时 worker 完全不调用评论
  路径；
- 评论读取复用既有 `GITHUB_TOKEN`（只读 PR 评论与 PR 元数据）；
- 日志与审计**不写评论正文**，只写 login、原因码、bundle 证据。

## 为什么免公网地址

飞书回调是**入站推送**：GitHub/飞书必须能主动找到本机，于是要么开 funnel 抢
端口，要么暴露一个需要额外鉴权的公网入口。PR 评论是**出站轮询**：worker 本来
就每 60 秒轮询 CI 状态，多读一个 issue comments 端点即可。没有入站面就没有
"回调地址失效 / 端口被占 / 隧道未监听"这类只在真实运行时才暴露的失败。

## 未做的部分（明确边界）

- **推送审批仍走终端**：推送发生在 PR 创建之前，没有 PR 可评论；本通道不覆盖
  它，`PushApprovalPort` 的行为不变；
- **飞书通道不变**：`HITL_APPROVER_IDS` 与飞书卡片回调的语义、路由、状态机转
  移表均未改动；`app/routes/**` 零改动；
- **两套身份命名空间不互通**：`HITL_APPROVER_IDS`（飞书 open_id）与
  `GITHUB_APPROVER_LOGINS`（GitHub login）之间**没有**任何回退或映射。飞书
  白名单里的人不会因为名字相同而被 GitHub 通道接受，反之亦然；
- **没有"评论即拒绝"**：只识别批准；拒绝仍走既有审批出口；
- **不处理已删除/编辑的评论**：只读当前评论列表，不做历史回溯。

## 后果

- 正面：远程合并审批第一次同时具备"可达"与"可验证身份"；外部动作（评论）
  本身留在 PR 上，可被人复核；
- 负面（如实）：多一个每 60 秒的 GitHub 读请求；白名单与 GitHub login 的运维
  归属需要人维护；PR 评论对机器人账号可见，不构成秘密信道；
- 边界：本切片只增加一条审批入口，不改变合并前 CI 门禁与 sha 乐观锁。
