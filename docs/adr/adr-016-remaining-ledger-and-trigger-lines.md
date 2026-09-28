# ADR-0016: 收尾台账——剩余登记项的触发线与下一轮方案

日期：2026-09-24
状态：已实施（登记类；两条小尾巴随本 ADR 一并清掉）
关联：ADR-014（收尾三态）、ADR-015（诚实收敛）、ADR-011（路由超时与意图一致性）

## 背景

第十、十一轮把"声明与实现不符"清零之后，剩余事项收敛为三类：
**登记未修**（litellm 未 await 协程）、**文档/环境小尾巴**、**明确不做与缺外部条件**。

按 ADR-014 的原则（每项落到"实现 / 删除 / 明确不做"三态、不留待办），光说"不做"还不够——
每一项都得写明**重新打开的条件（触发线）**。否则它们会在未来的盘点里被反复当成"新缺口"
（2026-09-24 的六维复评就发生过一次：README 已声明为有意边界的事项又被列进缺口清单）。

## 决策

1. **两条小尾巴本轮清掉**：

   - README 上下文预算条目的裁剪键名改为**实际落盘键**。此前写的是
     `kept/dropped/elided/tokens`，而 `context_budget` 实际落盘的是
     `enabled / history_in / history_kept / history_dropped / elided / summary_tokens /
     summary_truncated / budget` 八个键——措辞与事实对齐，别让读者照着错的键名去查。
   - CI 升级 `actions/checkout@v4→v5`、`actions/setup-python@v5→v6`（Node 24 原生运行时，
     消除"Node 20 弃用、被强制跑在 Node 24"的告警）；同时把两个 job 的
     `runs-on: ubuntu-latest` 钉为 `ubuntu-24.04`——GitHub 公告 ubuntu-latest 自
     2026-10-19 起迁移到 Ubuntu 26，钉住等于"迁移日零意外"（沿用今天的环境），
     想跟进 Ubuntu 26 时改那一行即可。

2. **litellm 未 await 协程：独立一轮（第十二轮），方案与验收先写死**。
   （**已完成，实施记录与验收结果见 ADR-017**；升级优先方案经 1.102.1 实验证伪，
   自修方案落地，四条验收全部满足。）
   现状：真跑 `evals/run_evals.py`（非 offline）时 stdout 抛
   `RuntimeWarning: coroutine 'OpenAIChatCompletion.acompletion' was never awaited`；
   已定位为 litellm 1.96.2 被**外部取消**时留下未 await 的内部协程（3 行复现、无本项目代码），
   触发点是 `app/router/intent_router.py:79,93` 两处 `asyncio.wait_for(classify(...), timeout)`。

   - **首选方案**：先试升级 litellm——若上游已修，一行依赖变更即可收工。
   - **自修方案**：把超时从"外部取消"改为"传进去"。给 `LLMClient.chat / chat_with_tools /
     _acompletion / _build_kwargs` 增加 timeout 参数（默认仍取 `LLMConfig.timeout`），
     `LLMIntentClassifier.classify` 透传，`IntentRouter` 把 `router_timeout_ms` /
     `micro_timeout_ms` 传下去后**移除两处 `asyncio.wait_for`**。
     已验证该路径可行：对 litellm 传自身 `timeout` 会干净抛 `litellm.Timeout`、无泄漏。
   - **验收标准（全部满足才算完成）**：① 真跑 e2e（非 offline）stdout 无该 RuntimeWarning；
     ② `intent_consistency` 热态下 stable/agreement 不低于现基线；③ 路由降级语义不变——
     冷启动仍降级到默认意图且 `route_fallback` 如实记录；④ 全量测试 0 回归。
   - **为什么独立成轮**：这是 ADR-011 的敏感区（"意图摆动"的真因就在路由超时），
     改超时语义必须带着它自己的验证走，不能搭车。

3. **明确不做 + 缺外部条件：逐项登记触发线**。到线之前它们不是待办；盘点时以本表为准，
   表外新发现才立项。

   | 项 | 触发线（什么条件出现才重新立项） |
   |---|---|
   | 回调 RBAC 角色校验 | 多角色共用同一套审批（当前白名单只答"能不能批"已够用） |
   | PR 审查写回 GitHub | 有真实仓库的消费者使用该链路（现为只读审查） |
   | OTel 真接线（OTLP exporter / collector） | 多实例部署或 >5 人日常使用 |
   | 评测基线对比（本次 vs 上次 delta） | 出现会消费评测结论的决策点——否则又是一个没人读的 JSON |
   | 语义级结果验证、LLM 摘要压缩 | "治理输出"定位变化或真实用户提出（与减法清单冲突） |
   | 成本量化（`avg_cost_usd` 恒 0） | 接入付费 provider |
   | `hitl_feedback` join 率与真实介入率 | 有真实流量（当前 4 条全为模拟种子） |
   | 微模型那一级路由 | 有第二个模型端点 |
   | 路由冷启动不降级 | 模型常驻，或演示前预热（操作说明已在 README） |
   | 多实例化（会话状态/预算累计器/限流器外部化） | 真要跑多实例（`redis_state/store.py` 是活的，按当时需求设计） |
   | 检索精排（rerank） | 检索 gold set 显示**排序**（而非召回）是瓶颈时——先有度量再谈精排，无 gold set 调精排是盲调（ADR-020 决策 5） |
   | 更换嵌入模型（如 bge-m3 1024 维） | gold set 显示**稠密腿**是瓶颈，且愿意付"全库重嵌入 + 重建 vector 列/索引"的迁移成本（`db/gateway_schema.sql:48-49` 已声明该约束；ADR-020 决策 4 的维度 fail-fast 是前置安全网） |
   | 知识库租户隔离 | 出现 >1 个租户/数据集需要分权时——**当前全仓无 `tenant_id` / `acl_groups`，知识库无隔离层**（`app/knowledge_access.py` 只是 DI port）；一旦引入，权限必须进两条腿的 SQL WHERE（ADR-020 决策 3） |
   | 工具写风险的审批机制（副作用分类 + 默认拒绝的允许表） | 出现**真正持久化 / 有外部副作用**的工具（落库、发消息、动钱）时——当前唯一"写"工具 `add_note` 只写进程内 `_NOTES_STORE`（重启即丢），为其建闸门是过度工程（ADR-018 决策 6） |
   | 分块策略升级（markdown / 代码感知 + breadcrumb） | 检索 gold set 显示**召回不足**，且定位到"定长 500 字符切碎结构化文档（标题/代码块）"是**原因**时——若瓶颈在排序/融合则不做（ADR-020 已用稀疏腿 + RRF 覆盖那类问题） |
   | ~~评测 judge 错误处理~~ **已完成（2026-09-27）**：provider 中途死亡时 judge 失败被逐条剔除计数（`judge_failures` 字段 + summary ⚠️ 标记），评测照常完成、报告照写；**绝不静默计 0**——0 分是"模型输出差"，judge 挂了是"量具没读数"，剔除让两者分得开 | ✓ 已销项（第十二轮验收时实测发现并立项，同日完成；用例 `test_judge_failure_is_counted_not_fatal_and_never_scored_zero` 钉住语义） |

## 后果

- 剩余事项**全部落账**：无未登记的开放项；后续盘点以本表为准，表外新发现才立项。
- CI 不再有第三方动作告警，runner 版本显式化。
- litellm 警告在第十二轮之前仍会出现在真跑 e2e 的 stdout 里（README 已知边界已注明）。

## 补记（2026-09-28）

外部七维架构审计 v2（2026-09-27，落在 `D:\WorkBuddyData\`）带来一批**表外新发现**，
按上表规则（"表外新发现才立项"）逐项处理，并复核了三条既有行的触发线。

**一、新发现已立项（新开三篇 ADR，不并入本表）**

| 新发现 | 立项 |
|---|---|
| `react.py:88` 的 `setdefault` 让模型自带的 `session_id` 压过系统值 → 跨会话读写笔记；工具参数 schema 从未进 decide 提示词（`litellm_llm.py:93-95`）；task-LLM 异常被折叠成 `status=ok` 的正常回答（`litellm_llm.py:42-47`）；`calculator` 无界且阻塞事件循环 | **ADR-018** |
| 审计"不可篡改"不成立（`wal.py` 明文 append、容量口径与落盘不符、满载静默丢最旧、`replay_all` 无业务消费方） | **ADR-019** |
| "混合检索"是过度声明（`fuse()` 是加权和、关键词腿只在稠密候选池内重排、无独立稀疏召回 / 无 FTS / 无 RRF）；中文用 PG 默认 FTS 会静默空；维度不符静默丢弃向量（`pgvector_client.py:509-517`） | **ADR-020** |

**二、三条既有行的触发线复核结论：全部未开。**

- **评测基线对比（delta 门禁）**：触发线"出现会消费评测结论的决策点"——**未开**。
  但审计同时暴露一个**缺陷**（不是这条被推迟的功能）：`evals/run_evals.py:241-256` 的
  `_OfflinePipeline` 连 pipeline 都不构造，CI 的 offline smoke **从未跑过业务链路**。
  **缺陷修，功能不立项**——修的是"CI 的 smoke 没冒烟到真链路"，不是"加上 delta 门禁"。
- **`hitl_feedback` join 率**：触发线"有真实流量"——**未开**（`real_cases=0`，没有真实用户，
  这是事实不是欠债）。join **率指标**不立项。

  **〔2026-09-28 更正本条的自述〕** 本行原先写"`app/channels/feishu.py` 在 trace 为空时用
  `session_id` 顶替，**导致** `unmatched_decisions=89`，这是错的实现，该修"。**复核后这个因果
  不成立**：`scripts/collect_hitl_feedback.py` 自己的注释就写着那 89 条是"②a（审计 trace 贯通）
  **之前**的历史决策无法关联"，是**数据窗口产物**；而卡片本来就嵌了 `trace_id`
  （`app/channels/feishu_cards.py:76,82`），正常点击能 join。用当前日志实测亦为
  `matched=0 / unmatched=89` 且**总数未增长**——自 2026-09-21 起就没有新的决策进来。
  所以**不存在"trace 兜底缺陷"**，无需修；原先的转述是错的，记录在此免得以后照着改一个
  不存在的东西。（顺带做了一次加固：回调不带 `trace_id` 时现在会打 warning，让"这次审批
  进不了评测"是可见的，见 `routes/feishu.py` / `routes/webhook.py`。）

- **多实例化（会话态外置）**：触发线"真要跑多实例"——**未开**。

**三、本表口径不变**：新增五行均为"明确不做 + 触发线"，到线之前不是待办。
"缺陷"与"被推迟的功能"必须分开记——前者修，后者等触发线；把两者混为一谈会让台账
要么掩盖缺陷、要么虚开功能。

**四、2026-09-28 追加裁定（当日复核后）**

- **分块策略升级（markdown / 代码感知 + breadcrumb）→ 登记为本表新行**（触发线见上表）。
  按"只修缺陷"的边界，它是**功能**而非缺陷：定长分块并没有"声称了没做到"，只是不够好。
- **幂等入库 → 撤回，不入账、不修**。原拟以 `UNIQUE(doc_id, content_hash)` 防重分块产生重复，
  但复核代码后该前提**不成立**：
  - `app/knowledge.py:70` 在写入前先 `delete_by_metadata({"doc_id": doc_id})`；
  - 分片 id 是**确定性**的（`f"{doc_id}:chunk:{i}"`，`:74`）；
  - `upsert_batch` 走 `ON CONFLICT (id) DO UPDATE`（`app/vectordb/pgvector_client.py:311`）。

  即**同一 `doc_id` 重入已经是幂等的**。而拟加的约束反而**有害**：两篇内容相同的**不同**文档
  会因 content_hash 相同而被拒——文本相同不等于同一篇文档。
  唯一残留的行为是 `add_document` 不传 `doc_id` 时用随机 id（`:67`），**每次调用即新增一篇**；
  这是"每次上传即一篇新文档"的合理语义，只值得在文档里点明，不构成缺陷。
  结论：**这是本次复核里被代码推翻的一条审计/计划主张**——记在此处，避免它下次又被当成缺口立项。

