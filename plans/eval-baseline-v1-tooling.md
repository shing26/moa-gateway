# Contract — issue #95 v1.2: 契约层 L0 裁决 + 基线运行器（第二次冻结）

**冻结时间：2026-11-16，在 verifier 第二轮 FAIL 之后、第三次裁决之前。**

诚实的历史，写在这里以免误读：

1. 实现先于任何契约存在；第一轮 verifier 按 build/test 命令裁决，给出 FAIL
   （缺 `stdin=DEVNULL`、9 条用例静默继承 `.env` 的 verify 命令、报告 `model` 恒空）。
2. 实现方修完后写第一版契约；第二轮 verifier 给出 FAIL，三条阻塞项全部是
   **契约本身的缺陷**，不是实现缺陷：
   - **B1** 我把 `ruff --isolated --select F401,F841,RUF100 .` 写成了全仓命令。
     仓库基线上该命令本来就有 337 条错误（`tests/unit/test_pr_workspace_pipeline.py`
     ×45、`app/orchestration/collaboration.py` ×12 ……），全部在 Allowed paths 之外。
     这条命令**在契约范围内不可能 exit 0**，是我的错。
   - **B2** 我把 `pytest -q`（含 `tests/integration`）写成全绿门槛。本机没有 compose
     起的 redis（`redis://localhost:6380`），且 `test_budget_guard` 一类时序断言在
     全量顺序下会翻（单独跑通过）。这些失败在基线上同样存在，与 diff 无关
     （verifier 用两种独立方式证明：引用 diff 代码的受跟踪文件只有三个；失败项单独跑全绿）。
   - **B3** 我写的 Forbidden 只允许 `subprocess` 上的 `# noqa`，实现里另有 3 处导入语句的
     `# noqa: E402`。这一点查出了真问题：仓库 ruff 配置不启用 E402，这些 `# noqa`
     是纯粹的多余指令。
3. 第三轮修订同时修了 verifier 的实质发现 P1（见下）与 P2（报告元数据、原子写、日志覆盖、
   `relative_to` 越界、`communicate` 无界）。**这不是为了让门变绿而放行**：
   B1/B2 改的是命令口径（全仓 → 改动文件；全量 → 单元套件 + 基线归因），
   改完仍然要求「本分支新增错误数必须为 0」，比原口径更严格；B3 与 P1/P2 是实打实的修补。

- 分支：`eval/baseline-v1-tooling`
- Base：`main` @ `d569a7db1b90b093b30ed51f96906cf3bdcf8361`
- 上游协议：[issue #95](https://github.com/shing26/moa-gateway/issues/95)

## Allowed paths

改动**必须恰好**是这些文件：

```
eval/l0.py                            (modified: +CONTRACT_CHECKS/+SHAPE_CHECKS,
                                       +contract_passed, +shape(); all_passed 只多 docstring;
                                       -1 行未使用的 `from typing import Any`)
scripts/run_eval_baseline.py          (new)
tests/unit/test_eval_l0.py            (modified, append only)
tests/unit/test_run_eval_baseline.py  (new)
plans/eval-baseline-v1-tooling.md     (new, 本文件)
```

## Verification commands

Verifier 亲自重跑。`$F` = 上面四个代码文件。

```
C1  python -m pytest tests/unit/test_eval_l0.py tests/unit/test_run_eval_baseline.py -q
C2  python -m ruff check eval/l0.py scripts/run_eval_baseline.py tests/unit/test_eval_l0.py tests/unit/test_run_eval_baseline.py
C3  python -m ruff check --isolated --select F401,F841,RUF100 eval/l0.py scripts/run_eval_baseline.py tests/unit/test_eval_l0.py tests/unit/test_run_eval_baseline.py
C4  python -m pytest tests/unit -q
```

- **C3 只跑改动文件**：仓库默认 ruff 配置忽略 F401（所以 C2 抓不到死代码，C3 才是补丁）。
  全仓 `--isolated --select F401,F841,RUF100 .` 在 HEAD 上是 **337** 条、在 base `d569a7d`
  上是 **338** 条；差的恰好一条是 base 的 `eval/l0.py: F401 'typing.Any' imported but
  unused`，即本次 diff 删掉的那一行。也就是说 **337 条全部在 Allowed paths 之外**，
  改动文件的贡献为 0。可复现：
  `python -m ruff check --isolated --select F401,F841,RUF100 . | tail -1` → `Found 337 errors`；
  在 base worktree 里同命令 → `Found 338 errors`。
- **C4 的裁决规则（不是"允许失败"，是归因要求）**：任何失败都要在 base `d569a7d` 的
  干净 worktree（`git worktree add .scratch/base d569a7d`）里复现；复现得到 → 记为既有失败，
  列进证据包，不阻塞；复现不到 → **阻塞**。本机已知既有失败：`tests/integration/*`
  需要 redis:6380 与 docker compose；`test_budget_guard` 一类时序断言在全量顺序下会翻。
  受跟踪文件里引用本分支改动代码的只有 `scripts/run_eval_baseline.py`、
  `tests/unit/test_eval_l0.py`、`tests/unit/test_run_eval_baseline.py` 三个。
- 独立参照：PR #96 的 CI `test` job 跑 `uv run pytest tests/unit -v`（见
  `.github/workflows/ci.yml`），必须在 head commit 上通过。verifier 可用
  `gh pr checks 96 --repo shing26/moa-gateway` 查看，但**不得**用它替代 C1/C4 的亲自重跑。

## Acceptance criteria

1. `L0Report.all_passed` 的行为与 base 完全相同（`git show d569a7d:eval/l0.py` 对照）：
   六项检查全过才 True。只允许 docstring 变化。
2. `L0Report.contract_passed` 当且仅当四个契约检查
   （`changed_paths_match`、`verify_exit_zero`、`forbidden_paths_touched`、
   `test_files_append_only`）全过时为 True；形状检查失败不影响它。
3. `L0Report.shape()` 返回 `{name: L0Check}`，仅含 `read_ratio`、`verify_cycles`。
   `L0Check` 仍是冻结 dataclass，字段 `name/passed/detail` 不变。
4. `check_run` 对语料中任意 record 形状不抛异常（既有测试全绿）。
5. `scripts/run_eval_baseline.py`：
   a. `build_command` 产出 `python -m app.cli delivery run` 的 argv，带
      `--base-sha <case.base_sha>`、`--origin manual`、`--source-ref <case.id>`、
      `--goal <case.goal>`、`--branch eval/<case.id>-<stamp>`、`--base-branch main`。
   b. `build_env` **总是**设置 `CHANGE_AGENT_VERIFY_COMMAND`（无 verify 时为空串），
      不留下 `.env` 默认值的任何通路；`ENV_OVERRIDES` 全部生效且不删除调用方其它键。
   c. `case_arm`：`live` / `gate_state` 二分。
   d. **gate_state 用例不启动子进程**：`run_case` 在 spawn 之前返回一行
      `arm="gate_state"`, `state="not_run_by_design"`, `run_id=""`, `exit_code=None`,
      非空 `reason`（说明 CLI 在 `app/cli.py:392-395` 以 exit 2 拒绝无 verify 的 run），
      且不写日志文件。理由是 `run_case` 会专门测这条路径且必须 0.5s 内返回。
   e. 子进程以 `stdin=subprocess.DEVNULL` 启动（源码断言）。
   f. 超时路径调用 `_kill_tree`（Windows `taskkill /F /T /PID`，异常被吞），
      随后的 `communicate` 有界（60s），并通过 `_find_run_id_for_branch` 补回 `run_id`。
   g. 报告为单一累积文件：`prepare_report` 保留旧 `rows`，但 `protocol`/`stamp`/`model`/
      `env_overrides`/`deferred_arm` **总是**由本次进程刷新；旧文件无 `rows` 时补空列表；
      写盘为 `.tmp` + `os.replace` 原子替换；日志文件名带 stamp（跨会话不互相覆盖）；
      `log` 字段在 `--report` 指向仓外时退回绝对路径而不抛 `ValueError`。
6. C1–C4 按上面的口径全部通过（C4 允许列出的既有失败，但每一条都必须有 base 复现证据）。
7. 运行器**不推送**：无 `git push`、无 PR 创建、无自动批准实现；唯一外部命令是 `taskkill`。
8. **被验证的树就是将被合并的树**：PR head 的 `tree.sha` 必须等于本地 HEAD 的
   `git rev-parse HEAD^{tree}`，且 `gh api repos/.../compare/<base>...<head>` 的文件列表
   必须与 `git diff --name-status <base>..HEAD` 逐条相等。第三轮 verifier 正是靠这条抓到
   推送脚本按单个 commit 的 `diff-tree` 重建远端分支，把第一个 commit 对
   `tests/unit/test_eval_l0.py` 的追加丢在了本地——远端少 46 行测试，PR 的 CI 从未
   覆盖它们。推送脚本已改为按 `<base>..HEAD` 全量 diff 重建，并在树的 SHA 不一致时
   直接失败。

## Forbidden

- 修改 `L0Report.all_passed` 的语义或实现。
- 修改 `eval/schema.py`、`eval/cases/**`、`app/**`、`.env`、lint 配置或 CI 配置。
- 为让 C3 变绿而加 `# noqa`；为让 C1/C4 变绿而 `xfail`/`skip`/删除断言。
- 在运行器里加入任何推送/合并/开 PR 的路径，或替换审批端口为自动批准。
- 把 `.scratch/`、`.worktrees/`、`data/`、`evals/reports/` 下的产物提交进本分支。

## 已知遗留（不属本契约范围，写进基线报告）

- **9 条 gate_state 用例本轮无测量**。它们是无 verify 命令的合成用例，`app/cli.py`
  在构造 request 之前就拒绝启动，因此既到不了推送门，也产不出 run 记录。
  协议里"26 条全跑"实际可达 17 条；9 条需要在语料里补一条中性的声明式 verify 命令，
  或另建 gate-state 测试台。这一步会改动策展数据，需要单独一次带 verifier 的改动。
- **模型在 v1.4 改为 `nvidia/nemotron-3.5-super-vl-120b-a12b`**（#95 记录）。v1.2 的
  `poolside/laguna-xs-2.1` 只经连通性探测就被录取，第一次真实冒烟即被证伪：121 步全是
  `read_file`、零写入、1.37M tokens、`budget_exhausted`。新模型是台账 v19（28/28）用过的那一个，
  探测确认能在数秒内发出 `write_file` 工具调用。
