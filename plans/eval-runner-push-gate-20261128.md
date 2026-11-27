# 契约：评测 runner 推送闸门配置修正

- **task id**：`eval-runner-push-gate-20261128`
- **分支**：`fix/eval-runner-push-gate-20261128`（worktree `.worktrees/fix-eval-gate`）
- **冻结时间**：2026-11-28
- **所有者签收**：用户消息「开小 PR 修 runner → 关掉 #169–#175 和 eval 分支 →
  abandon 这批评测单 → 重跑 K=3」

## 1. 现象（有证据）

2026-11-27 23:57 起的 `--rounds 3` 实跑，21 条 live 用例里：

- 3 条停在 `ready_for_push_approval`，2 条 `needs_human/approval_timed_out`；
- 5 条墙钟 ≥1400s，最长 2511.2s（v2 基线单条为分钟级）；
- 真正推出去的 7 条各自开了一个 PR（#169–#175），远端留下 9 个 `eval/*` 分支；
- 用例日志里是 `push approval: feishu (timeout=1800s)`。

## 2. 根因

`scripts/run_eval_baseline.py` 的 `ENV_OVERRIDES` 只钉了
`CHANGE_AGENT_PUSH_ENABLED=1`，没有覆盖 `FEISHU_PUSH_APPROVAL_ENABLED`。子进程
继承 `.worktrees/run-main/.env` 的 `=1` 与 `FEISHU_PUSH_APPROVAL_TIMEOUT_S=1800`，
于是每条越过推送门的用例都会发一张真飞书审批卡，然后阻塞等人点或等超时。

评测进程没有 TTY、也没有审批人，推送门本来就该停在终端端口（无 TTY 立即拒绝），
让终态落在 `rejected` 供 L0 打分。

## 3. 允许路径（Allowed paths）

```
scripts/run_eval_baseline.py
tests/unit/test_run_eval_baseline.py
plans/eval-runner-push-gate-20261128.md
```

超出此清单的一律先回主会话重新冻结。

## 4. 验收标准（Acceptance criteria）

- **A1 进程级关闭**：`ENV_OVERRIDES["FEISHU_PUSH_APPROVAL_ENABLED"] == "0"`。
- **A2 敌意父环境不泄漏**：父环境显式给 `=1` 时，`build_env()` 返回 `"0"`。
- **A3 其余覆盖不变**：模型、推送开关、预算、超时、编码等既有键值零改动。
- **A4 `.env` 不动**：不改仓库或 worktree 的 `.env`，本修正只作用于评测子进程。
- **A5 无副作用**：不新增依赖，不改 `app/cli.py`、`delivery` 包与飞书路由。

## 5. 验证命令（Verification commands）

```
V1  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit/test_run_eval_baseline.py -q
V2  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit/test_delivery_cli.py tests/unit/test_feishu_route.py -q
V3  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m ruff check .
```

Verifier 需亲自重跑，不采信实现方贴的日志。

**V3 复审记录（2026-11-28 03:46，验证阶段发现，非实现变更）**：初版 V3 写的是
显式路径 `ruff check scripts/run_eval_baseline.py tests/unit/test_run_eval_baseline.py`。
该命令与仓库既有 `pyproject.toml` 的 `extend-exclude = ["scripts"]` 冲突——在
`ssh443/main` 上同样报 `BLE001`（`except Exception` 盲捕获，2026-11-16 引入），
即命令本身永远无法通过，与被测改动无关。改为仓库真实的 CI 门 `ruff check .`；
`scripts` 不在扫描范围内，故本单不承担该既有告警。若要对 `scripts` 收紧 lint，
另立一单，不在本契约内。

## 6. 禁止（Forbidden）

- 改 `.env` 或 `app/config.py` 里 `FEISHU_PUSH_APPROVAL_*` 的默认值。
- 在评测路径上新增“自动批准”分支（等于绕过人审）。
- 改/删/弱化既有断言换通过；跳测试不记录。
- 顺手重排 `ENV_OVERRIDES` 或改动无关行。

## 7. 合并后仍需做（不在本单）

- 关闭 #169–#175 与 9 个 `eval/*` 分支；
- 用 `delivery abandon` 收口本轮 13 条评测单；
- 删除被污染的 `evals/reports/eval-multiround-v1.*` 后重跑 K=3。
