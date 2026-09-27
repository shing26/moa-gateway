# ADR-0017: 路由超时从"外部取消"改为"客户端内执行"

日期：2026-09-27
状态：已实施
关联：ADR-011（意图一致性与降级可见性）、ADR-016（剩余台账，本项由其登记立项）

## 背景

真跑评测时 stdout 抛 `RuntimeWarning: coroutine 'OpenAIChatCompletion.acompletion'
was never awaited`。定位（ADR-016 登记）：litellm 被**外部取消**时会留下未 await 的
内部协程——3 行复现（仅 `litellm.acompletion` + `asyncio.wait_for`，无本项目代码），
且 **1.102.1（当时最新稳定版）依然未修**。触发点是 `app/router/intent_router.py` 的两处
`asyncio.wait_for(classify(...), timeout)`：意图路由用 2s 超时从外部掐掉慢模型调用，
而本地小模型冷启动实测 4.2s（ADR-011），于是每次路由调用都在制造一个泄漏协程
——live 评测一次 100 次一致性重放 = 100 条警告，且取消让模型热不起来、后续继续超时。

## 决策

1. **升级优先方案先行并证伪**：试装 litellm 1.102.1 复跑 3 行复现——警告仍在，上游未修；
   回滚到 1.96.2（锁文件不动），不把无关变更带进来。
2. **自修**：把超时从"外部取消"改为"传进去"——
   - `LLMClient.chat / chat_with_tools / _acompletion / _build_kwargs` 增加
     `timeout` 参数（缺省仍取 `LLMConfig.timeout`）；
   - `LLMIntentClassifier.classify(text, *, timeout_s=None)` 透传给 `chat`；
   - `IntentRouter` 把 `router_timeout_ms` / `micro_timeout_ms` 传下去，
     **删除两处 `asyncio.wait_for`**（全仓库仅有的两处外部取消点）。
3. **语义变化（已写明并测试钉住）**：`timeout` 是**每次尝试**的上限（litellm 传给 HTTP
   客户端），fallback 链最坏 (1+N)×timeout，不再是"整次调用"的总上限——路由 LLM 未配
   fallback 模型，实际等价。超时以 `litellm.Timeout` 异常落地，走与原 `wait_for`
   TimeoutError 相同的 `except Exception` 降级路径：**对外语义不变**（超时 = 没拿到判定
   = 默认意图 + `route_fallback="none"` 如实记录）。
4. **测试改造**：路由桩按 litellm 行为模拟超时（超时即抛 `TimeoutError`），新增一条
   "classify 把 timeout 透传进 chat"的钉子。

## 验收（ADR-016 写死的四条，全部满足）

1. **真跑 e2e 无警告**：`run=30 skipped=0 success=1.0`，全程 stdout `RuntimeWarning` 计数 **0**。
2. **一致性不低于基线，且从"假稳定"变成真测量**：基线（外部取消时代）stable=1.0 但
   **degraded=100/100**——全部调用被取消后降级成默认意图，报告自注"稳定率不代表判断质量"。
   本轮：**stable 0.95（19/20）/ avg_agreement 0.99 / degraded=0**——100 次重放全部真实
   到达路由 LLM 并拿到判定，警示注记不再出现。1 例不稳定属热态下路由不完全逐位确定
   （ADR-011 已记录的已知边界）。
3. **降级语义不变**：超时路径单测钉住；`route_fallback` 可见性字段不受影响。
4. **零回归**：798 passed / 0 skipped；ruff、bandit、红队、offline eval 全绿。
   附带实测：judge 0.7117 / 平均延迟 4452.8ms（1.5b 模型，与上轮 0.75/4766ms 同量级）。

## 过程发现（已另行登记）

- **评测 judge 无错误处理**：验收首次运行时本地 Ollama 进程死亡（环境问题），agent 链路
  按既有重试+降级优雅处理，但 `evals/judge.py:23` 的裸 `chat` 让 `InternalServerError`
  一路穿透评测 CLI——**整个评测崩溃、报告都不写**。已登记进 ADR-016 台账（能落码、小时级）。
- Ollama 进程死亡属外部环境（弱 GPU 机器的已知风险），与本轮改动无关。

## 后果

- litellm 保持 1.96.2；其"外部取消泄漏协程"的上游缺陷仍在，但**本仓库已无该调用方式**。
- 路由超时的执法点从路由层移到 LLM 客户端层；`RouterLLM` 协议签名带上 `timeout_s`。
- 演示建议不变：本地小模型冷启动仍可能超时降级，上调 `ROUTER_LLM_TIMEOUT_MS` 或演示前预热。
