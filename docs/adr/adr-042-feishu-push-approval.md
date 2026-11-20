# ADR-042: 飞书远程推送审批——阻塞等待而不是新增状态

日期：2026-11-20
状态：已实施（A1 第一切片）
关联：ADR-021（合并审批通道）、ADR-030（受治理变更）、ADR-031（受治理仓库交付）、
`plans/a1-feishu-push-approval-20261120.md`

## 背景

A2（2026-11-19）解决了「审批能不能算数」：审批人身份可验证、approver ≠ operator、
不满足时 fail-closed 并写审计。但**推送审批**仍然只有
`TerminalPushApprovalPort`：要求真终端、要求本人在推送者机器前打出 `APPROVE`。
后果是第三人无法在终端外批准，服务器与 CI 上也无法完成审批——这是
「第二个人在自己的仓库上跑通」的唯一硬阻塞。

合并审批的飞书回程已经存在（`app/routes/github_delivery.py` +
`MergePoller` / `MergeNotifier` + `_handle_merge_approval`），缺的是
**PR 创建之前**的那道推送审批。

## 决策

### 1. 复用 `PushApprovalPort`，不新增状态

飞书端口实现既有协议：

```python
async def request(*, run_id, bundle) -> ChangeApproval | None
```

端口在 `READY_FOR_PUSH_APPROVAL`（`DeliveryFlow` 已落盘）阻塞等待，默认上限
1800 秒、2 秒轮询：

- 批准 → 返回 `ChangeApproval`，CLI 继续推送；
- 拒绝 / 超时 / 不可用 → 返回 `None`，既有 flow 落 `REJECTED`。

**不新增 FSM 状态、不改返回类型、不改合并闸门。** 备选方案是给端口加一个
「pending」返回类型并由回调恢复执行，但那要求改 `DeliveryFlow` 的状态语义、
新增恢复路径与测试面；第一切片选择阻塞等待，把改动限制在适配器内。

代价是 CLI 进程最长被占用一个 timeout；进程被杀时 run 停在
`READY_FOR_PUSH_APPROVAL`，pending 记录按 TTL 失效，需重新 `delivery resume`。
这个代价换的是「零状态机改动」，在单机单操作者场景可接受。

### 2. 跨进程靠共享 Redis，降级即拒绝启用

CLI 与飞书回调服务是两个进程：

1. CLI 写 pending（复用 `SessionStore.store_hitl`，`hitl_kind="push_approval"`，
   载荷放在新增的 `HitlRequest.delivery_payload`）；
2. 回调经白名单门禁后 `pop_hitl` **原子认领**，再写 decision key；
3. CLI 轮询 decision key，读到决定后返回。

`RedisHitlStorage` 在 Redis 不可用时会降级为进程内存。此时 CLI 写的 pending
回调进程看不见，审批会变成「点了没反应」。因此端口在发卡前**主动探测**存储，
一旦处于内存降级就拒绝启用并报明确原因——跨进程看不见的审批等于没有审批。

### 3. 回调只记录决定，推送仍由操作者机器执行

服务器端不持有操作者 PAT 的推送语义，也不应替操作者推送。回调只写 decision；
`ChangeApproval` 由 CLI 在读到决定后构造，推送仍走既有 `ChangePublisher`
（建分支/commit/PR，禁止 force push 与删分支）。

### 4. bundle 绑定必须回读 run store

卡片上的 `bundle_hash` 是**声明**，不是真相。回调批准前必须回读
`delivery_run_store` 中该 run 的当前 `run.bundle.bundle_hash`，与 pending
记录里的值比对；不等即拒绝（`bundle_mismatch`），并写审计。

### 5. 身份与自审批

- 审批人必须属于 `HITL_APPROVER_IDS`（复用 `approver_gate_error`，
  空白名单 fail-closed）；
- `FEISHU_OPERATOR_OPEN_ID` 是操作者自己的 open_id。配置后 approver 与之相同
  → 拒绝（`self_approval`）；未配置 → 拒绝（`operator_unverified`）；
- 只有显式 `GATEWAY_ALLOW_INSECURE=1` 才放行，并在审计 `gate` 里如实标记
  `insecure_*`。

终端通道的「自称身份不可验证」不适用于飞书：飞书回调的 `operator_id` 来自
平台，`identity_verified=True` 是有依据的。

## 后果

- `FEISHU_PUSH_APPROVAL_ENABLED` 默认关；关的时候终端通道行为逐字不变。
- 新增 `FEISHU_PUSH_APPROVAL_TIMEOUT_S`、`FEISHU_OPERATOR_OPEN_ID`、
  `DELIVERY_APPROVAL_CHAT_ID`（未配置时沿用 `CODE_REVIEW_MERGE_CHANNEL`）。
- 合并审批的飞书通道与 GitHub 评论通道均保留，互不改写。

## 已知边界

- 单机假设：`delivery_run_store` 是本地目录，CLI 与回调服务需在同一文件系统；
  跨机需共享存储，另立契约。
- 加密的飞书事件仍不支持（明确 400），不在本切片。
- 强模型通过率、探索/写入预算分离属于 B 阶段，不在本切片。
