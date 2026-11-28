# 契约：评测 runner 钉死降级链（单一模型臂）

- **task id**：`eval-fallback-pin-20261128`
- **分支**：`fix/eval-runner-fallback-pin-20261128`（worktree `.worktrees/fix-eval-fallback`）
- **冻结时间**：2026-11-28
- **所有者签收**：用户消息「选择前者」（在「钉死 fallback」与「保留链并改名」之间选前者）

## 1. 现象（有证据）

2026-11-28 03:45 起的 K=3 第二批，第一例 `biz-050`：641.4s、115 次 LLM 调用、
1,048,253 prompt tokens、终态 `budget_exhausted`；日志 92 次 429，
`nvidia/nemotron-3.5-super` 失败 63 次、`minimaxai/minimax-m3.1-flash` 被拉起 29 次。

## 2. 根因

`app/cli.py:459` 用 `LLMConfig.from_env("LLM")` 构造客户端，只覆盖 `model`；
`app/agents/provider.py:263` 用 `[primary, *fallback_models]` 逐个尝试。因此真正
生效的降级链是 `LLM_FALLBACK_MODELS`，`CHANGE_AGENT_FALLBACK_MODELS` 无读取方。
`ENV_OVERRIDES` 没钉前者，臂的标签（单一冻结模型）与实际执行不符。

## 3. 允许路径（Allowed paths）

```
scripts/run_eval_baseline.py
tests/unit/test_run_eval_baseline.py
docs/acceptance/eval-multiround-preregistration.md
plans/eval-fallback-pin-20261128.md
```

## 4. 验收标准（Acceptance criteria）

- **A1 钉空**：`ENV_OVERRIDES["LLM_FALLBACK_MODELS"] == ""`。
- **A2 敌意父环境不泄漏**：父环境给非空值时，`build_env()` 返回 `""`。
- **A3 语义可证**：同一环境下 `LLMConfig.from_env("LLM").fallback_models == []`。
- **A4 既有覆盖不变**：`CHANGE_AGENT_MODEL`、`CHANGE_AGENT_PUSH_ENABLED`、
  `FEISHU_PUSH_APPROVAL_ENABLED`、预算与超时等键值零改动。
- **A5 预注册留痕**：§10 A1 记录缺陷、证据、适用范围与副作用（`infra_error` 预计上升）。

## 5. 验证命令（Verification commands）

```
V1  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit/test_run_eval_baseline.py -q
V2  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit/test_delivery_cli.py tests/unit/test_agents.py -q
V3  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m ruff check .
```

Verifier 需亲自重跑，不采信实现方贴的日志。

## 6. 禁止（Forbidden）

- 改 `.env`、`app/agents/provider.py` 或降级逻辑本身。
- 在评测路径上新增“429 自动换模型”。
- 修改 `eval/l0.py` 判据、用例语料或已完成的历史读数。
- 改/删/弱化既有断言换通过；跳测试不记录。

## 7. 合并后（不在本单）

- 先跑 3 例试点（含至少一条会走到推送门的用例）测 429 / `infra_error` 率；
- 试点可接受再起 K=3（预计 ~20h）；试点不可接受则回头谈配额，而不是偷偷开降级链。
