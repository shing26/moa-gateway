# ADR-024: PR Review 价值闭环，候选与发布分离

日期：2026-10-20
状态：已实施
关联：`apps/code_review_pipeline`、`ReviewStore`、`evals/review_value.py`

## 背景

原来的 PR 审查链路是五段顺序调用：Triage、Static、Semantic、Coverage、Report。
它能把结果跑出来，却没有回答三个价值问题：

1. Report 的每条 finding 是否真的经得起质证；
2. 人工是否能逐条告诉系统“有效、误报、重复、已知”；
3. Critic 和一次改进到底改善了什么，又增加了多少成本与延迟。

如果直接把 Report 的输出写回 GitHub，系统只能证明“发出了评论”，不能证明
“评论有业务价值”。

## 决策

1. **候选与发布分离。** Report 先产出 candidate；Review Critic 逐条给出
   `keep / revise / drop`。`revise` 只允许回 Report worker 一次，最终再走
   evidence gate。Critic 自身失败有界重试，失败后让任务失败，不静默退回 R0。
2. **Finding 有稳定身份和 lineage。** `finding_uid` 绑定 trace、来源和确定性词袋
   fingerprint（不是 embedding）；`code_review_findings` 保存供审批消费的当前快照，
   `code_review_finding_events` append-only 保存候选、修订、最终、Critic 丢弃和
   superseded 状态，修订通过 `revision_of` 串联。人工反馈使用独立事件表，重跑
   快照不会删除反馈或 finding lineage 历史。同一 `trace_id` 的每次 worker 执行
   另生成 `review_run_id`，所有飞书反馈与终止动作都在数据库条件更新中校验它，
   防止重跑期间旧卡片操作新快照。
3. **人工反馈是五态事件。** 固定为 `accepted_actionable`、
   `accepted_non_actionable`、`false_positive`、`duplicate`、`already_known`。
   `false_positive` 与 `accepted_non_actionable` 必须带 reason code；Feishu
   `event_id` 是幂等键，最新人工事件生效，模型预标不计入完成度。
4. **所有最终 finding 标注完才允许写回。** 飞书先发分页逐条卡，卡片上的
   批准/拒绝只控制 GitHub review 写回；它不控制合并通道。写回固定 `COMMENT`，
   永远不发送 `APPROVE`，也不调用 merge API。
5. **证据优先，不设模型阈值。** 没有 evidence 的 finding 即使 Critic 说 keep
   也只能落为 dropped；evidence 必须解析到 diff、workspace 文件行或确定性工具
   输出，解析通过才写 `evidence_verified=true`。模型 confidence 和字符串形状都
   不是可验证证据。
6. **评测只允许一轮预声明改进。** `scripts/run_review_value.py` 在固定模型、温度 0、
   相同 token/workspace 上限下执行 R0、R1、R1+I。先锁定 24 个公开 PR 与 12 个
   单缺陷受控 case，再按 recall、precision、重复/已知、非行动项、evidence 具体度
   的固定顺序选一项改进；复跑不改口径、不挑样本。报告逐 case 对照 manifest 的
   base/head SHA；`R1+I` 必须声明同一个 predeclared improvement code，且只能有
   一个 changed path。runner 的 finding disposition 默认留空，模型输出不能冒充
   人工终审。独立模型预标只进入嵌套 `model_prelabel`；实际模型集合必须与配置
   完全一致，token 超限、case 级 workspace 超时或出现任何 fallback 都使该 run
   失败，不能进入对照报告。

## 后果

- 这条链路仍然是**人工审批的 PR 审查助手**，不是自动合并门禁；`COMMENT` 写回
  不会改变 PR 的 mergeability。
- 飞书反馈闭环生产默认开启，但 GitHub 写回仍需人工点击控制卡；合并审批保持独立。
- 24 个公开 PR 是离线历史语料，不是持续用户；只有用户逐条终审的结果能进入
  human disposition。
- 若 R1 或 R1+I 指标变差，报告必须如实保留负结果和改进假设，不能通过改标签或
  换样本来制造正结论。
