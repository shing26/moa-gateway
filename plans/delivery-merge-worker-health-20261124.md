# 契约：合并 worker 的心跳、健康检查与启动

- **task id**：`delivery-merge-worker-health-20261124`
- **分支**：`feat/merge-worker-health-20261124`
- **背景**：2026-11-23 的 `dlv-effee4ed01d64e0fa2c6ef8e867325ba` 已推送、
  PR #133 CI 全绿，却**长时间没有任何审批卡**。根因不是代码分支，而是
  `python -m app.merge_worker` 这个独立进程从未启动：`app/main.py` 只调用
  `merge_channel_readiness()` 做配置自检，不启动 `MergePoller`。ADR-021 明确
  合并通道是独立进程（失败域隔离），所以本契约不把 worker 塞进网关，而是让
  「worker 没起来」这件事**在 5 秒内可见**，并给出可执行的启动方式。

  同时修一个同族的静默失败：`merge_notifier.py` 用
  `from app.deps import _card_sender as _global_card_sender`，在模块导入时就把
  `None` 绑死了。`init_feishu()` 之后再赋值 `app.deps._card_sender` 也不会更新
  这个别名，于是 webhook 进程里每次 PR 打开都打印
  `feishu card sender not initialized; skip PR opened card`，PR 打开通知从未发出。

## 1. 语义

1. **心跳**：worker 每完成一轮轮询，向 Redis 写一个心跳键
   `moa:merge_worker:heartbeat`，值为 JSON
   `{"pid": int, "interval_s": float, "at": "<UTC ISO8601>"}`，
   TTL = `max(60, 3 * interval_s)`。写失败只记 warning，**不得**中断轮询。
2. **健康检查**：`/healthz` 新增 `merge_worker` 项，语义三态：
   - 合并通道关闭（`CODE_REVIEW_MERGE_ENABLED=0`）→ `disabled`（健康）；
   - 开启且心跳年龄 ≤ `3 × interval_s` → `running`（健康）；
   - 开启但心跳缺失/过期/读取失败 → `degraded: ...`（把整体 status 拉成
     `degraded`）。
   该项**任何异常都不得让 `/healthz` 返回 5xx**，只能降级成字符串。
3. **启动自检**：`merge_channel_problems()` 增加一条「worker 心跳」前置条件；
   `app/main.py` 在 lifespan 里把心跳年龄传进去，缺失时打 ERROR 并明确写出
   启动命令 `python -m app.merge_worker`。自检本身仍不得阻断启动。
4. **卡片发送器**：`MergeNotifier` 在**调用时**读 `app.deps._card_sender`，
   不再使用导入期别名；显式注入的 `card_sender` 仍然优先。
5. **启动方式**：`docker-compose.dev.yml` 增加 `merge-worker` 服务（同一镜像，
   命令 `python -m app.merge_worker`，依赖 redis/postgres 健康），本地开发
   一条 `docker compose -f docker-compose.dev.yml up -d` 即含该进程。

## 2. 实现要求

- 心跳实现放进 `app/merge_worker.py`：新增可注入的 `heartbeat` 可调用对象
  （async，无参），默认实现用 `settings.redis_url` 建 `redis.asyncio` 客户端；
  测试注入假实现，不碰真 Redis。
- `MergeWorker.__init__` 新增 `heartbeat=None` 参数；`_run_once()` 末尾调用，
  异常吞掉并 `logger.warning`。
- 心跳读取逻辑抽成 `apps/code_review_pipeline/merge_preflight.py` 里的
  `read_worker_heartbeat_age_s(...)`（返回 `float | None`，绝不抛异常），
  供 `/healthz` 与启动自检共用。
- healthz 的检查放在 `checks` 里参与 `all_healthy` 判定；`disabled` 与
  `running` 都加入 `healthy_values`。
- 不修改 `MergePoller` / `MergeExecutor` / 合并状态机语义。
- 不新增 Python 依赖（`redis` 已在依赖里）。

## 3. 允许路径（Allowed paths）

```
app/merge_worker.py
app/routes/health.py
app/main.py
apps/code_review_pipeline/merge_preflight.py
apps/code_review_pipeline/notifications/merge_notifier.py
docker-compose.dev.yml
tests/unit/test_merge_worker.py
tests/unit/test_merge_preflight.py
tests/unit/test_merge_notifier.py
tests/unit/test_health_merge_worker.py
plans/delivery-merge-worker-health-20261124.md
```

> **修订 1（2026-11-24，Verifier P1 追加）**：允许路径追加 `.env.template`，
> 仅用于把 `--profile merge` 的旧启动说明改成默认启动。原始冻结内容不变。

## 4. 验收标准

- **T1 心跳写入**：`MergeWorker._run_once()` 成功后调用心跳一次；心跳抛异常
  时轮询结果仍返回、异常不外泄（有测试）。
- **T2 healthz 三态**：enabled + 心跳新鲜 → `merge_worker=running` 且
  `status=healthy`；enabled + 无心跳 → `degraded:` 前缀且 `status=degraded`；
  disabled → `disabled` 且 `status=healthy`。
- **T3 healthz fail-soft**：心跳读取抛异常（Redis 不可用）时
  `/healthz` 不抛 5xx，`merge_worker` 为 `degraded: ...`。
- **T4 启动自检**：`merge_channel_problems(..., worker_heartbeat_age_s=None)`
  在通道启用时返回含 `app.merge_worker` 的问题条目；心跳新鲜时不返回该条目；
  通道关闭时不返回。
- **T5 卡片发送器**：`init_feishu()` 之后新建的 `MergeNotifier`（未显式注入）
  能拿到 `app.deps._card_sender`；显式注入优先；`app.deps._card_sender` 为
  `None` 时跳过并记 warning，不抛异常。
- **T6 启动方式**：`docker-compose.dev.yml` 有 `merge-worker` 服务且
  `command` 为 `python -m app.merge_worker`，依赖 redis/postgres。
- **T7 不回归**：既有合并相关单测全绿；`MergePoller` / `MergeExecutor` /
  `merge_state.py` 无 diff。

## 5. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_merge_worker.py tests/unit/test_merge_preflight.py tests/unit/test_merge_notifier.py tests/unit/test_health_merge_worker.py tests/unit/test_merge_poller.py tests/unit/test_merge_executor.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

## 6. 禁止

- 把 `MergeWorker` 放进网关进程（违反 ADR-021 的失败域隔离）。
- 让心跳写入失败中断轮询，或让心跳读取异常把 `/healthz` 打成 5xx。
- 把 `disabled` 当成 `degraded`（合并通道关闭是合法部署）。
- 修改 `MergePoller` / `MergeExecutor` / `merge_state.py`。
- 新增 Python 依赖；删除或弱化既有断言。

## 7. 签收记录

**所有者书面签收**：用户消息「签收 #134 #135」（2026-11-24）。

**合并**：PR #135 → `main`，merge commit
`cb32179cdc13ec9072d4a2a0ea02ba93d5cb0818`。

**验收证据**：独立 Verifier `ACCEPT`（HEAD `718d5eb`），T1–T8 全 PASS，
无 P0/P1。V1 `59 passed`；V2 `2334 passed, 6 skipped`；V3
`All checks passed!`。`docker compose config` 中 gateway 与 merge-worker 的
`CODE_REVIEW_MERGE_ENABLED` / `CODE_REVIEW_MERGE_POLL_INTERVAL_S` 取值一致，
`merge-worker.restart=no`，`.env.template` 不再出现 `--profile merge`。

**上线状态（2026-11-24 收口核实）**：代码已合并，但 8083 网关与
`python -m app.merge_worker` 仍运行旧代码，`/healthz` 尚无
`merge_worker` 三态；需重启两者后才算真正上线。

## 8. 修订 1：部署可用性（Verifier P1）

独立 Verifier 在 `be92e82` 上判 `ACCEPT_WITH_NITS`，指出两个 P1。它们不改变
T1–T7 的结论，但会让「默认 `up -d` 就能收到审批卡」这个目的落空，所以合并前
必须闭合：

1. **worker 默认崩溃循环**：compose 给 `merge-worker` 注入
   `GITHUB_TOKEN: ""`，`build_worker()` 直接抛
   `RuntimeError: GITHUB_TOKEN is required for Code Review Pipeline`，而
   `restart: unless-stopped` 会把它变成无限重启。
   要求：缺 token 时容器**一次性退出**并留下清晰错误（`restart: "no"`），
   不制造重启风暴；其余情况下仍随 compose 常驻。
2. **网关侧看不到合并通道**：gateway 服务未注入
   `CODE_REVIEW_MERGE_ENABLED` / `CODE_REVIEW_MERGE_POLL_INTERVAL_S`，
   默认读 `false`，于是 `/healthz` 恒为 `disabled`，worker 缺失永不被暴露。
   要求：gateway 与 `merge-worker` 两个服务注入同一组
   `CODE_REVIEW_MERGE_ENABLED`、`CODE_REVIEW_MERGE_POLL_INTERVAL_S`，
   保证健康阈值与 worker 实际间隔一致。
3. `.env.template` 中 `--profile merge` 的说明改为默认启动。

**追加验收（T8）**：

- `docker compose -f docker-compose.dev.yml config` 中，gateway 与 merge-worker
  的 `CODE_REVIEW_MERGE_ENABLED` / `CODE_REVIEW_MERGE_POLL_INTERVAL_S` 取值一致；
- `merge-worker` 的 `restart` 不为 `unless-stopped`（避免缺 token 时重启风暴）；
- `.env.template` 不再出现 `--profile merge`；
- T1–T7 全部仍然通过（不改既有实现语义）。
