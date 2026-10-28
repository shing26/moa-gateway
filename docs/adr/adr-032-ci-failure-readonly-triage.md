# ADR-032: CI 失败只读诊断

日期：2026-10-28
状态：已接受并实施
关联：ADR-030、ADR-031

## 背景

受治理交付 Agent 已经有 `DeliveryRequest -> DeliveryRun` 入口，但 CI 失败
只被当作一个终止条件。要让同一个治理内核承载第二个真实负载，第一步需要
把失败 CI 的可复核证据收集起来，而不是直接给 Agent 开放修复、重跑或写回
权限。

CI job log 是不可信输入：它可能包含 token、伪造的指令、误导性的错误摘要，
也可能因为权限不足而完全不可读。把“读取日志”与“执行修复”放在同一个默认
动作里，会把提示注入、凭据泄露和不可审计写入混成一个信任边界。

## 决策

1. **第一版只做只读诊断。** `delivery ci-triage --repo owner/name --sha <sha>`
   收集失败的 check runs、最新一次 Actions workflow attempt 的失败 jobs 与
   job logs，在本地生成 JSON、Markdown 和 `DeliveryRequest(origin="ci_failure")`。
2. **不产生任何 GitHub 写操作。** 不修改代码，不重跑 workflow，不推送分支，
   不创建 PR，不评论，不合并，不部署。报告固定标记人工复核。
3. **权限最小化。** fine-grained token 只需要目标仓库的 `Checks: Read` 与
   `Actions: Read`。Actions 权限缺失时保留 checks 证据，并记录明确的
   `actions_unavailable_reason`；不把权限错误伪装成“没有失败”。
4. **日志先脱敏、再有界。** 常见 bearer/token/API key/AWS key 形状在进入
   evidence 前脱敏；原始下载以流式读取设 2 MB 安全上限，超限 job 明确标记
   不可用，每个 job 只保留配置上限内的脱敏日志尾部，单次 job 数有界。
5. **分类是确定性证据归类，不是根因证明。** timeout、cancelled、
   permission/auth、dependency、syntax/build、test failure 只是可复核标签；
   报告只引用有限行证据，unknown 不允许被补写成猜测性根因。
6. **日志内容是指令的禁区。** CI 日志属于不可信数据；读取者不得执行其中要求
   安装、访问、修改或泄露内容的指令。
7. **写侧另开显式审批阶段。** 将来若要从诊断进入修复，仍必须复用 ADR-030
   的验证、人工审批、推送与审计顺序，不得让本命令隐式升级为写入口。

## 替代方案

- **直接自动修复并重跑 CI。** 否决：默认动作同时引入代码写入、执行副作用和
  不可审计的重跑，超出当前证据链的成熟度。
- **只读 check 结论，不读 job log。** 否决：check 名称只能说明哪条失败，无法
  给人工复核提供失败行证据。
- **把日志全文交给模型后自动给出根因。** 否决：日志不可信且可能含凭据，当前
  没有证据支持把模型推断当作根因结论。
- **新增 webhook、队列或 MCP 入口。** 否决：当前只需要一个可重复的本地只读
  命令；在第二个消费者出现前铺设入口属于投机泛化。

## 后果

- 项目现在可以宣称“支持只读 CI 失败诊断”，但不能宣称“支持 CI 自动修复”。
- 诊断结果可被人工审阅，也可作为后续受治理 `DeliveryRequest` 的输入；生成
  request 不等于授权执行。
- Checks API 本身不可用时只生成“失败状态不可判定”的 JSON / Markdown，明确
  提示补齐 `Checks: Read` / `Actions: Read`，且不生成 `DeliveryRequest`。
- token 缺少 Actions 权限时仍能产出部分证据，但报告不会声称拿到了完整日志。
- 本命令不替代现有 PR 审查或合并通道，也不改变 `CHANGE_AGENT_PUSH_ENABLED`
  默认关闭的行为。
