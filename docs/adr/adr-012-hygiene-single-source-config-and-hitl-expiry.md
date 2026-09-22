# ADR-0012: 仓库卫生、配置单一来源、HITL 失效语义与意图正则精度

日期：2026-09-23
状态：已实施
关联：ADR-009（错误契约与配置 fail-fast）；ADR-010（生命周期）；ADR-011（一致性维度）；
`5d29545`（CI 当场变红暴露的"同一概念两个开关"缺陷类别）

## 背景

外部缺口规划（2026-09-23）把 `5d29545` 修掉的那条当作**缺陷类别**而非偶发，扫出同类实例。
逐条实测后确认四项，其中三项是真实的、一项的前提不成立（见"后果"）。

## 决策

1. **仓库卫生（唯一"不做会立刻出事"的一项）**。`.gitignore` 首行带 UTF-8 BOM，使
   `__pycache__` 规则**从未生效**；而 `!/*/` 会 un-ignore 所有顶层目录，于是
   `git add -A` 一次会带进 **483** 个文件——包含 `.workbuddy/` 的会话记忆（坦率内容）。
   修法：去掉 BOM、把首行改为 `__pycache__/`、在 `!/*/` **之后**显式忽略
   `.workbuddy/` / `.mimosa/` / `.zcode/` / `__pycache__/`（顺序是关键：负向规则在后，
   后写的忽略规则才生效）。实测 `git add -A --dry-run` 从 483 降到 1。

2. **配置单一来源**。同一个配置概念被多处各自读 env，且**接受集合或优先级不同**——
   已实测三例：

   | 概念 | 读取点 | 分叉 |
   |---|---|---|
   | HITL 开关 | `app/config.py` vs `graph._hitl_enabled()` | `HITL_ENABLED`(默认 false) vs `MOA_HITL_ENABLED`(默认 true) |
   | embedding 维度 | `app/config.py` vs `rag/embeddings.py` vs `storage/review_store.py` | 前者优先 `VECTOR_DB_*`，后两者**反向**优先 `CODE_REVIEW_*` |
   | embedding 凭据 | 同上 | 后两者只认 `CODE_REVIEW_EMBEDDING_API_KEY`，config 的链是 `EMBEDDING_API_KEY → CODE_REVIEW_* → OPENAI_API_KEY` |

   只设通用名 `EMBEDDING_API_KEY` 时，`app/vectordb` 拿得到 key 而 code review 路径拿到空串
   → **静默 401**；docstring 还承诺了"or OPENAI_API_KEY"。修法：三个读取点统一走
   `app.config.settings`（与 `app/vectordb` 已有的注入式写法一致），非正整数的 fail-fast
   交给 config 层（ADR-009 的原则）。

3. **防类别，不只是修实例**。新增 `tests/unit/test_config_consistency.py`：对每个配置概念
   断言**只有 `app/config.py` 读 env**（`scripts/doctor.py` 例外——它按设计检查原始 env）。
   另有 "`MOA_HITL_ENABLED` 不能再出现" 的化石守卫。这才是"CI 抓到一个 → 我防住一类"。

4. **HITL 失效语义**。`HitlRequest` 落在 Redis（带 TTL），而 FSM 会话状态只在**进程内**
   `_session_states`。服务重启后挂起记录还在、状态已丢，此时 `INIT + HUMAN_APPROVED`
   是非法迁移——此前表现为 webhook 路径 **500**、飞书路径"处理审批时出错了"，而且用户
   重试仍然失败（卡片变成点一次错一次的砖）。修法：`Engine.decide_hitl()` 返回
   `(状态, 是否失效)`，两个回调入口共用（语义不允许漂移）；失效时**消耗掉**挂起记录、
   明确告知用户重新发起、写 `guard_action="hitl_expired"` 审计。

   **前置已核**：飞书触发侧（`feishu.py` `sid = parsed["chat_id"]`）与回调侧
   （`:143` `parsed["chat_id"]`）的 session_id **同值**，状态恢复不会找错会话。

5. **意图正则精度**。`task` 正则里裸写 `算` 会命中「预**算**表」「打**算**」
   「核**算**」这类复合词——实测「预算表里的数字对不上」与「这活儿我算下来要三天」都被
   吞成 `task`（后者把一句普通陈述送进 TaskAgent）。收紧为动词短语
   （`计算|算一下|算一算|算算`），真计算请求（「算一下 37*89」）仍命中。

## 后果

- 测试 742 → **753 passed / 8 skipped**；`ruff`、`bandit` 干净；红队 200 条 100% / 0 误拦；
  `eval --offline` 退出码 0；活体 e2e `run=30 skipped=0 success=1.0`。
- 验收对照：A1（`git add -A` 483→1）✅、A2（只设通用名两条路径都拿到 key）✅ 实测
  `None → 'Bearer sk-ONLY-GENERAL'`、A3/A4（维度同源 + 守卫 6 条）✅、
  A5/A6（失效不 500 + `hitl_expired` 审计）✅ 单测覆盖、A8（≥726 passed）✅ 753。
- **规划里 A7 的前提不成立**："意图一致率 54% → ≥90%" 这个目标基于"判定不稳定"的推断，
  而 ADR-011 已查明真因是**路由 LLM 超时竞态**（`ROUTER_LLM_TIMEOUT_MS=2000` vs 冷启动
  4.2s → 静默降级到默认意图）。预热后实测 55/55 一致（`stable_rate` 1.0），完整活体跑
  0.9091 且报告自带 `degraded 2` 警示。**该目标既已达到、也不该用这个口径衡量**。
- 已知未修（同类别但本轮未动）：`review_store._dsn()` 也各自读
  `CODE_REVIEW_DATABASE_URL / DATABASE_URL / POSTGRES_URL`，未与 config 的 DSN 链统一；
  守卫目前只覆盖 embedding 与 HITL 两个概念，DSN 未纳入。
