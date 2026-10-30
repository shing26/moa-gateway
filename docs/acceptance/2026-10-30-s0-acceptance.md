# 受治理仓库交付 S0 真实验收记录

- 日期：2026-10-30；文中时间戳均为 UTC（北京时间 = UTC+8）
- 结论：**通过**。GitHub、DeliveryRun、MergeRecord、审计 WAL 四方在 PR、head SHA、bundle hash、trace ID、run ID、审批人、merge SHA 上一致。
- 本文件自身亦通过同一受治理链路交付（见 §7）。

## 1. 范围

真实执行完整链路并留证：

`DeliveryRun -> 确定性验证 -> bundle 推送审批（人） -> GitHub 分支/提交/PR -> CI -> 合并审批（人） -> MergeExecutor 合并 -> 证据对账`

无模拟、无替身、无绕过。

## 2. 交付运行（r4）

- run_id：`dlv-250277d39d824e5cb6f4d147c54fa0b6`
- trace_id：`delivery-1793336181522167800`
- 仓库与基线：`shing26/moa-gateway`，base = `main` @ `ce679a3f473aef9e594ce745bc9460778468b336`
- 交付分支：`moa/s0-acceptance-20261030-claude-r4-delivery-1793336181522167800`
- 目标：创建 `docs/acceptance/2026-10-29-delivery-smoke.md`，内容为一句固定语句
- 实现模型：`nvidia/nemotron-3.5-super-vl-120b-a12b`（进程级覆盖，见 §6）
- 确定性验证（部署方预注册命令，断言文件内容与固定语句逐字一致）：exit 0，输出 `acceptance smoke ok`，output_hash `9c01cca7856a11861433cd2814da0d5225d969b62e7a43193ead1c1ccccaae69`
- bundle：新增 1 个文件；patch_hash `946101b65c5db5a7bebbeb9ce3c067de5154f2e959638598662583f86d86f479`；bundle_hash `184cf86f2ef05618ea18c1ba1f3a1bc794da6344dfee2f5bdc408c880ac9b061`
- LLM 用量：3 次调用，3323 tokens，端到端时延约 51.8s

## 3. 推送审批（人，TTY）

- 审批端口：`TerminalPushApprovalPort`（TTY 交互，非交互 stdin 自动拒绝）
- 操作者：`shing26`，本人在终端输入身份并键入 `APPROVE`
- 审计：该 trace 的 `human_decision` 条目 @ 2026-10-30T05:01:34Z
- 结果：分支与提交推送，PR 创建：https://github.com/shing26/moa-gateway/pull/15 ，head `116283516b65490465c811f0bd8948bb8dcc97a2`（DeliveryRun 进入 `waiting_ci` @ 05:01:41Z）

## 4. CI 与合并（治理路径）

- CI（head commit）：`test` -> success；`docker` -> skipped
- `MergePoller.poll_once()`：`watching` -> `awaiting_approval`（ci_green）@ 05:03:25Z
- 合并审批：人在会话中提供操作者身份后调用 `MergeExecutor.execute("shing26/moa-gateway#15@116283516b65490465c811f0bd8948bb8dcc97a2", "approve", "shing26")`；未使用 `gh pr merge`
- MergeRecord 转移：`watching` -> `awaiting_approval`（ci_green）-> `merging`（approve，05:04:45Z）-> `merged`（complete，05:04:49Z）
- GitHub：merged=True，merged_at `2026-10-30T05:04:48Z`，merge_commit_sha `54052491aa033cb38ba6beb8773c08e7f5847088`

## 5. 证据对账

| 字段 | GitHub | DeliveryRun | MergeRecord | 一致 |
|---|---|---|---|---|
| PR | #15 | #15 | pr_number=15 | ✓ |
| head SHA | 1162835… | 1162835… | 1162835… | ✓ |
| merge SHA | 54052491… | — | merged_sha=54052491… | ✓ |
| bundle hash | — | 184cf86f… | 184cf86f… | ✓ |
| trace / run | — | delivery-1793…/dlv-2502… | 相同 | ✓ |
| 审批人 | — | —（TTY，审计在案） | approver=shing26 | ✓ |

- 审计 WAL：`logs/audit-2026-10-30.jsonl` 中该 trace 共 7 条（6 × delivery_lifecycle，1 × human_decision）
- 哈希链：`chain_report` -> `broken_at=None`，chained_lines=7（链完整，无断裂）

## 6. 披露（诚实性声明）

1. `.env` 中默认 `CHANGE_AGENT_MODEL` 在 NVIDIA chat 端点返回账户级 404；本次运行以进程级环境变量覆盖为 `nvidia/nemotron-3.5-super-vl-120b-a12b`（经项目 `LLMClient` 实测可用）。`.env` 文件未做任何修改。
2. `CHANGE_AGENT_PUSH_ENABLED=1` 同为进程级覆盖；仓库 `.env` 保持 0。
3. NVIDIA 调用设置 `NO_PROXY=integrate.api.nvidia.com`：本机系统代理当前指向不可用 stub。
4. MergeStore 使用本机 PostgreSQL 17.6（localhost:5433）而非 Docker：本主机 Docker Desktop 因 Windows 重解析点（reparse point）故障不可靠；CI 的 `docker` job 在本仓库为 skipped。
5. 前次尝试 r3（run `dlv-02a4fe12071c46c0afb097b6e39f1834`）已到达 `ready_for_push_approval`（验证 exit 0），但交互会话在推送审批前结束，未推送、未建 PR；其 patch_hash 与本次一致（`946101b6…`），仅 bundle 元数据因分支名不同而不同。
6. 编排代理从未冒充审批人：推送审批由人在 TTY 亲自输入；合并审批的操作者身份由人在会话中显式提供；两者分别落于审计 WAL 与 MergeRecord。
7. 初版交付（run `dlv-d795c4fcd1de4277…`，PR #16）因交付启动脚本的编码缺陷（无 BOM 的 UTF-8 被 Windows PowerShell 5.1 按 GBK 解析）整体乱码入库，而当时仅断言 ASCII 哈希串、未能发现该损坏。2026-10-31 经同一条受治理链路更正（dogfood 台账 #1），验证命令自此改为对全文 SHA-256 的断言——该缺陷与发现过程本身记入台账。

## 7. 本记录的交付方式

本文件通过同一受治理链路交付：新的 `delivery run`（确定性验证命令断言本文件包含全部关键证据标识）-> 人在 TTY 推送审批 -> CI -> 人在会话中批准、`MergeExecutor.execute` 合并。其 DeliveryRun 记录与 MergeRecord 见运行环境 `data/delivery-runs/` 与 `pr_merge_gate` 表，可按 §5 同样方式对账。
