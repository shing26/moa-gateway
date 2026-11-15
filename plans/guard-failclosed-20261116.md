# 契约：输出守卫 fail-closed（guard-failclosed-20261116）

状态：**已冻结**（2026-11-16，冻结后不得修改；实现偏离须回到本会话重新冻结）
裁决方：主会话（审计）；实现方：独立 worktree Implementer；验证方：全新 verifier subagent

## 背景与决策（ADR-037 的素材）

`guard_service.evaluate_output`（输出守卫）的异常路径在三个站点把失败当放行：

- `app/pipeline.py:588`
- `app/orchestration/graph.py:579`
- `app/orchestration/collaboration.py:1454`

三处均为 `except Exception: output_verdict = GuardVerdict(action=GuardianAction.ALLOW, reason="ok")`。
治理系统最该 fail-closed 的位置是 fail-open，且无 ADR。

**决策**：异常时 verdict 改为 `GuardianAction.REVIEW`（三态语义见
`app/guard/guard_service.py:29`——REVIEW 走 HITL 审批卡），reason 必须携带
失败信息（异常类型与简要消息），不得吞掉异常细节。**保持宽捕获**：守卫自身
崩溃不应当炸掉请求主流程（可故障转移），但必须升级人工——这就是 fail-closed
在本系统的具体含义。正常路径（evaluate_output 正常返回）行为**不得有任何变化**。

## Allowed paths（仅限以下文件）

- `app/pipeline.py`、`app/orchestration/graph.py`、`app/orchestration/collaboration.py`（三站点修复）
- `pyproject.toml`（仅 `[tool.ruff.lint] ignore` 列表：移除 `E722`、`F821`）
- 因开启 F821 而须补 import 的文件：`apps/code_review_pipeline/agents/code_review_pipeline.py`
  （`AgentEnvelope`）及 F821 报出的其余站点——修法是**补正确的 import**，
  禁止删改代码或加 `# noqa: F821`
- 新增/修改的回归测试文件（`tests/unit/` 下）
- 新增 `docs/adr/adr-037-fail-closed-output-guard.md`

## Acceptance criteria（Verifier 逐条判定）

- **A1** 三站点的 except 路径 verdict 为 `REVIEW`，reason 含异常信息；不允许
  抛出异常终止请求（故障转移语义），不允许改回 ALLOW。
- **A2** 正常路径行为不变：现有单测全绿（1998+ 通过基线只增不减）。
- **A3** 新增回归测试至少覆盖 pipeline 站点（构造 `evaluate_output` 抛异常 →
  断言最终 verdict action 为 REVIEW）；graph/collaboration 站点说明一致性
  覆盖方式（双运行时 parity/golden 门禁或直接测试均可，须写明）。
- **A4** `pyproject.toml` 的 ignore 移除 `E722` 与 `F821`；开启后全仓 ruff 通过。
- **A5** Verification commands 全部通过。
- **A6** 证据绑定 HEAD SHA 与 `git status --short`。

## Verification commands（Verifier 亲自重跑，不采信转述）

```bash
uv run pytest tests/unit -q
uv run ruff check .
uv run ruff check . --select E722,F821   # 必须零命中，证明规则真实生效
uv run bandit -r app -q -s B105,B107,B110,B112,B324
uv run python -c "import app.main"
```

附加静态判据（Verifier 用 Grep 完成）：

```text
app/ 内 "GuardVerdict(action=GuardianAction.ALLOW" 仅允许出现在
app/guard/guard_service.py:126（未配置守卫时的文档化默认值，超出本契约范围）；
三站点文件中不得再出现该模式于 except 块内。
```

## Forbidden

- 不得修改 `docs/acceptance/dogfood-ledger.md`、其他 ADR、README
- 不得修改 `app/guard/` 下任何策略语义（`guard_service.py:126` 的默认值不在本契约范围）
- 不得跳过、删除或弱化任何现有测试断言
- 不得触碰 `tests/integration/`
- 不得关闭任何 lint/typecheck/门禁
- pyproject.toml 除 ignore 列表外不得改动其他键
