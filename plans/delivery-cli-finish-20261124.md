# 契约：交付 CLI 收尾三项

- **task id**：`delivery-cli-finish-20261124`
- **分支**：`feat/delivery-cli-finish-20261124`
- **背景**：产出周剩下三项都落在 `app/cli.py` 与 `apps/.../delivery/`，分开做会
  三次撞同一文件，所以合成一份契约、一个分支、一次独立验证。三项互不依赖：

  1. `apps/code_review_pipeline/delivery/retry.py:247` 里有
     `from app.cli import _build_push_approval_port`——**领域层反向依赖 CLI**。
     `delivery` 被 `eval/`、测试与未来的独立入口复用，这个 import 让任何
     "不进 CLI 就跑 retry" 的尝试都要拖进整个命令行模块。
  2. `delivery run` 结束时只在 `approval_timed_out` 一态提示下一步
     （PR #132 做的）；`FAILED` / `BUDGET_EXHAUSTED` / `NEEDS_HUMAN` 仍然只打印
     状态，人要自己记住该用 `resume` 还是 `view`。
  3. `delivery view` 只 `json.dumps`；终端里读不出 PR、head_sha 与 CI 失败项。

## 1. 语义

### T1 推送审批端口迁出 CLI

1. 新增 `apps/code_review_pipeline/delivery/approval_port.py`：
   - `TerminalPushApprovalPort` 从 `app/cli.py` **原样迁入**（行为、文案、
     fail-closed 分支、`identity_verified` 语义全部不变）；
   - `build_push_approval_port(github, *, settings_obj=None)` 承接原
     `_build_push_approval_port` 的选择逻辑（默认终端，显式开启才走飞书）。
2. `app/cli.py` 保留同名符号（`TerminalPushApprovalPort`、
   `_build_push_approval_port`）作为**再导出**，因为
   `eval/gate_state.py` 与 `tests/unit/test_delivery_cli.py` 从 `app.cli` 导入；
   再导出不得复制实现。
3. `retry.py` 改为从 `apps.code_review_pipeline.delivery.approval_port` 导入，
   删除 `app.cli` 依赖。
4. 验收：`rg "from app\.cli|import app\.cli" apps/` 无输出。

### T2 `delivery run` 结束提示

`cmd_delivery_run` 打印完 `record` 之后，按终态追加一行（不改退出码）：

| 状态 | 追加行 |
|---|---|
| `failed` / `budget_exhausted` | `resume    : delivery resume --run-id <id>` |
| `needs_human` | `view      : delivery view --run-id <id>` |
| `ready_for_push_approval` + `approval_timed_out` | `resume    : …`（既有行为） |
| 其他 | 不追加 |

### T3 `delivery view` 可读摘要

1. **stdout 是 TTY 时**，把摘要块写到 **stderr**（stdout 保持纯 JSON）：
   `run_id` / `state` / `failure`（code + message）/ `pr`（#号 + URL）/
   `head_sha` / `ci`（状态 + 失败检查名，最多 5 个）/ `merge`（state + merged_sha）。
2. **stdout 非 TTY（管道/测试）时不打印摘要**：`json.loads(stdout)` 恒成立。
3. JSON 内容与字段顺序不变。

> **修订 1（2026-11-24，Verifier REJECT 后）**：原设计把摘要写在 JSON 之前，
> 一旦 `failure.message` 含 `{`，「从第一个 `{` 解析」就失效（Verifier 已复现）。
> 改为「人看的摘要走 stderr、机器看的 JSON 独占 stdout」，既保住脚本契约，也
> 不再依赖任何分隔符约定。

## 2. 允许路径（Allowed paths）

```
apps/code_review_pipeline/delivery/approval_port.py
apps/code_review_pipeline/delivery/retry.py
apps/code_review_pipeline/delivery/feishu_approval.py
app/cli.py
tests/unit/test_delivery_approval_port.py
tests/unit/test_delivery_cli.py
tests/unit/test_delivery_retry.py
plans/delivery-cli-finish-20261124.md
```

## 3. 验收标准

- **T1** 新模块含端口与工厂；`app/cli.py` 只再导出；`apps/` 内无 `app.cli`
  导入；端口行为不回归（既有拒绝/放行测试全绿）。
- **T2** 三种终态各打印对应提示，其他状态不打印；退出码与 #132 的规则一致
  （`waiting_ci`=0、无 failure 的 `ready_for_push_approval`=0、其余=1）。
- **T3** stdout 在任何情况下都是纯 JSON（`json.loads(stdout)` 恒成立，
  包括 `failure.message` 含 `{`）；TTY 时 stderr 摘要包含 `/pull/`、head_sha、
  失败检查名；非 TTY 时 stderr 无摘要。
- **T4** 不新增依赖；不删弱化断言；`ruff check .` 全绿。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_delivery_cli.py tests/unit/test_delivery_retry.py tests/unit/test_delivery_approval_port.py tests/unit/test_delivery_resume.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

> **修订 2（2026-11-24）**：`python` 一律指仓库 venv 解释器
> `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`（Python 3.13）。
> 用系统/Anaconda 的 bare `python` 会因 bench 子进程 `isolated_env` 白名单不含
> `APPDATA` 而看不到 user site，产生 14 个与本分支无关的
> `ModuleNotFoundError: No module named 'fastapi'`。V2 必须在项目 venv 下执行。

## 5. 禁止

- 改变审批身份校验、bundle 绑定或 fail-closed 语义。
- 让 `delivery view` 在非 TTY 下输出非 JSON（脚本契约）。
- 改变既有退出码或把提示写进 stderr（`resume` 提示是 stdout 的一部分）。
- 触碰 Allowed paths 之外的模块；新增依赖。

## 6. 签收记录

**所有者书面签收**：用户消息「签收 #136」（2026-11-24）。

**合并**：PR #136 → `main`，merge commit
`0cd88280064faf8a43591b2b5ef56a4f66c14590`。

**验收证据**：独立 Verifier `ACCEPT`（HEAD `fb109fe`），T1–T4 全 PASS，
无 P0/P1。V1 `80 passed`；V2 `2343 passed, 6 skipped`；V3
`All checks passed!`。真实 PTY 走查：stdout 为纯 JSON（`failure.message`
含 `{"a": 1}` 时仍可 `json.loads`），stderr 摘要含 `/pull/`、head_sha、
check-0..4。
