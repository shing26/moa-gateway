# ADR-022: 多 Agent 协作（R6 多级 supervisor）立项

日期：2026-10-09
状态：已实施
关联：ADR-008（LangGraph 第二引擎，保持冻结）、ADR-016（剩余台账：精排与分块触发线）、ADR-020（混合检索与 gold set 验收）、`delivery/定位与减法清单.md`

## 背景

R6（多级 supervisor）在 `delivery/高层架构设计.md` 与 `delivery/UserStory.md` 里
被列为**延后到完整版的增强项**，理由当时是"MVP 先跑通分发与闭环，多级协作为增强项"。
随后《定位与减法清单（2026-09-22）》把这条线连同任务 Agent、长期记忆一起
归入"冻结：保留代码、停止扩展"，并把"双引擎"列为"不再给它添场景"。

冻结在当时是对的：项目已经承认"看起来全能、用起来智障"，继续加功能只会稀释深度。
但冻结留下了一个**没有回答的问题**：一个只能"选一个 Agent 再跑"的系统，与 JD 上
反复出现的"多 Agent 协作 / 任务规划与调度 / Reflection"之间隔着什么。答案是：
它缺的不是又一个 Agent，而是**Agent 之间如何协作**的机制。

## 决策

1. **只解除 R6 这一条**，其余冻结项不动。任务 Agent、长期记忆、Web Chat 仍是
   "同一编排层的示例应用"；PR 审查垂直应用仍是"只读审查"。解除的是
   "**多 Agent 协作作为一条能力线**"，不是"把冻结全部推翻"。
2. **协作链路是新增路径，不进入请求主路径**。理由：
   - 意图路由的 `INTENT_AGENT_MAP` 与 `_AGENT_KEYS` 自检是既有契约，
     把协作接进去要改动路由与它的测试面，属独立议题；
   - `test_langgraph_adapter.py` 的 `MODELLED_COLLABORATORS` 漂移门禁钉的是
     `MoAPipeline.__init__` 的协作者表面。协作链路做成**独立模块**，
     不动 `MoAPipeline.__init__`，那条门禁自然保持绿色；
   - 于是 ADR-008 的等价性对照适配器与本文的协作图是**两件不同的事**：
     一个用代码回答"主流框架能否实现同样的约束"，一个**提供能力**。
3. **形态**：Supervisor 规划 → 按子任务 handoff 到专家（复用
   `AGENT_REGISTRY` 里的 coder/general/review）→ Critic 评审 → 有界 revise 回边
   → 汇总 → 既有 guard/HITL/审计。这是 `delivery/research_report.md` 已经调研过
   的 LangGraph supervisor 模式，不是新范式。
4. **运行时**：LangGraph `StateGraph`（可选 extra，CI 的 `--all-extras` 已覆盖）。
   FSM 继续承担单 Agent 请求路径；协作是**第三种**编排路径。
5. **治理复用，不新写**：专家执行走 `execute_with_retry`，输出走
   `RuleEvaluator` + `_merge_guard` + `_verdict_from_eval_issues`，最终输出走
   guard/HITL，每子任务发一条共享 `trace_id` 的审计条目。差异化叙事因此是
   "**多 Agent 协作在治理之下**"——这仍然是那个治理层。
6. **有界与终止**：`COLLAB_MAX_ROUNDS`（默认 2）与 `COLLAB_MAX_SUBTASKS`
   （默认 4）双重上限。达到上限仍不通过时标 `need_human_review`，走既有
   guard REVIEW → HITL；HITL 关闭时交付并显式标注"critic 未通过"，不伪装成成功。
7. **离线确定性**：`MockCollaborationLLM`（规则式 plan/critique）与
   `MockTaskLLM` 同构，CI 零网络零 token 跑完整协作图。
8. **RAG 线按 ADR-016/020 的触发线顺序推进**：先建 retrieval gold set 与
   Hit@k/MRR/nDCG 度量，再加精排并给前后对比；分块升级保持冻结，
   除非度量显示瓶颈是召回而非排序。

## 验收

- 协作图可编译，`node_path` 形状稳定，checkpointer resume 可用；
- mock 规划/handoff 归属/critic 打回只重跑 targets/有界终止/达上限走
  review-HITL/失败重试升级人工/成本与工具计数聚合正确；
- 协作链路下 deny/review/allow 三态与单 Agent 路径一致；
- retrieval gold set 与 `run_retrieval_eval` 落账，Hit@k/MRR/nDCG 离线可跑；
- rerank off/on 在 gold set 上的前后对比可复现；
- 现有 parity/golden 与漂移门禁**不改且保持绿色**。

## 后果

- 项目从"治理层，不是全能 agent"变为"治理层 + 一条可演示的多 Agent 协作线"。
  前者仍是差异化（每个 Agent 的输出都过闸门），后者补上 JD 要的能力面。
- 代价：多一条编排路径、一个新模块、一个评测维度。边界由第 2 条约束住。

## 实装结果（2026-10-10）

- `app/orchestration/collaboration.py`：`CollabOrchestrator`，一张
  `plan → dispatch →(Send fan-out)→ execute_expert → critic →(有界回边)→
  summarize → evaluate → guard → prepare_hitl → await_human → deliver` 的图，
  外加 `blocked` / `rejected` / `failed` 三个收口节点。
- 入口：`POST /api/v1/collab`、`GET /api/v1/collab/pending/{trace_id}`、
  `POST /api/v1/collab/approve`（鉴权沿用 `/api/` 前缀本来就在的白名单），
  以及 `python -m app.cli collab` 与 `scripts/run_collaboration.py`。
- 测试：`tests/unit/test_collaboration.py` 22 条 + `test_collab_routes.py` 10 条，
  全离线零 token。全量单测 1084 passed / 4 skipped（基线 1042/4）。
- 检索度量落账：`docs/retrieval-evaluation.md` 记了离线基线与精排 delta
  （六个指标无一为正，如实记下）。

### 实装中定下的边界（原决策未涉及，补记于此）

1. **协作不驱动会话 FSM**。`prepare_hitl` 若向从未 `MESSAGE_RECEIVED` 过的会话
   发 `NEEDS_HUMAN`，`INIT + NEEDS_HUMAN` 是迁移表里没有的一跳；若与请求路径
   共用 `session_id`，两个写者会抢同一个会话状态（per-session 锁存在的理由）。
   协作挂起因此活在 LangGraph checkpoint，由 `resume()` 用 `Command(resume=...)`
   放行。飞书卡片回调**不**服务协作。
2. **失败升级 + HITL 关闭 = `status="error"` + `error_code="agent_failed"`**，
   与 `LangGraphOrchestrator._node_execute` 同一条门。这一眼容易看漏：
   漏了它，"自动处理失败"会以 `status=ok` 交付。
3. **checkpoint 里只放 JSON 原生类型**。`plan` / `verdict` / `critique_history`
   存 dict，dataclass 只在代码边界用 `to_dict()` / `_assignments()` /
   `_to_verdict()` 转换。自定义类型会被 LangGraph 的未来版本 block，现在就要绕开。
4. **`depends_on` 只记录不调度**：当前图把所有 pending 子任务一次性 fan-out，
   不按拓扑排序。要支持"B 等 A"需要分层调度，属独立议题。
5. **CI 只门禁"检索维度真的跑过"，不设质量阈值**。23 条 gold set 上一行标注
   就是 4.3 个百分点，拿单次实测当阈值会让 CI 在合法增删标注时误红。
   加入条件写在 `docs/retrieval-evaluation.md`。
