# Contract — 非 TTY 启动时推送门必须判 rejected，不得抛 EOFError

**冻结于实现之前**（2026-11-16）。上游证据：基线 v2 报告 §3.3
（`docs/acceptance/eval-baseline-v2.md`）。

## 缺陷

`app/cli.py` 的 `TerminalPushApprovalPort.request` 这样守门：

```python
if not sys.stdin.isatty():
    print("non-interactive stdin; push approval denied")
    return None
operator = (await asyncio.to_thread(input, "approver: ")).strip()
```

在本机 Windows Python 3.13 上，用 `stdin=subprocess.DEVNULL` 拉起的子进程
`sys.stdin.isatty()` 返回 **True**，所以守卫不触发，`input()` 读到 EOF 抛 `EOFError`。
`DeliveryFlow` 把审批端口的任何异常映射成 `NEEDS_HUMAN` + `FailureCode.INFRA_ERROR`
（`apps/code_review_pipeline/delivery/flow.py:363-369`），于是终态是
`infra_error: EOFError: EOF when reading a line`，而不是设计里的
`REJECTED` / `APPROVAL_REJECTED`（`flow.py:375-383`，`approval is None` 分支）。

真实事故：`sec-ledger-selfwrite` 首跑 `dlv-…`（baseline2）就是这样结束的。
没有安全后果（`input()` 立刻 EOF，没人能批出 `APPROVE`），但**终态语义错误**，
且真实原因会被「基础设施错误」掩盖。

## Allowed paths

恰好这两个文件：

```
app/cli.py                            (只允许改 TerminalPushApprovalPort.request 的方法体)
tests/unit/test_delivery_cli.py       (append only：只能追加测试)
plans/nontty-push-gate-20261116.md    (本文件，新增)
```

## Acceptance criteria

1. `TerminalPushApprovalPort.request` **从不抛异常**。以下三种输入都必须返回 `None`
   并打印一行拒绝说明：
   a. `sys.stdin` 为 `None`（pythonw / 无标准输入）；
   b. `sys.stdin.isatty()` 为假（既有行为，不得改变）；
   c. `isatty()` 为真但读取 stdin 立即 EOF（`input()` 抛 `EOFError`）——本缺陷的情形。
2. 拒绝说明里必须含 `non-interactive stdin` 或 `stdin closed` 之一，且**不得**暗示
   发生了基础设施故障（不出现 `error`/`exception` 字样）。
3. **正常路径不变**：`isatty()` 为真、操作者名非空、第二行恰为 `APPROVE` → 返回
   `ChangeApproval`，`run_id/trace_id/bundle_hash` 与入参一致；操作者名为空或第二行
   不是 `APPROVE` → 返回 `None`（既有测试必须原样通过）。
4. 新增回归测试：
   a. 单元级——伪造一个 `isatty()→True` 且第一次 `input()` 就抛 `EOFError` 的 stdin，
      断言返回 `None` 且打印了说明；
   b. 进程级——用 `stdin=subprocess.DEVNULL`（本缺陷的真实条件：该子进程
      `isatty()` 为 True）真拉起一个子进程调用该端口，断言它以 0 退出且**打印了拒绝说明**
      而不是抛出 `EOFError`。这条测试的意义是把「DEVNULL 在 Windows 上不是 TTY」这个
      平台事实钉死，防止有人把它改回去。
5. 既有测试一条都不许改（`tests/unit/test_delivery_cli.py` 只能追加）；
   `tests/unit/test_delivery_cli.py` 全部通过。
6. 不新增依赖，不新增 CLI 参数，不引入环境变量开关。

## Verification commands

```
V1  .venv/Scripts/python.exe -m pytest tests/unit/test_delivery_cli.py -q
V2  .venv/Scripts/python.exe -m ruff check app/cli.py tests/unit/test_delivery_cli.py
V3  .venv/Scripts/python.exe -m ruff check --isolated --select F401,F841,RUF100 app/cli.py tests/unit/test_delivery_cli.py
V4  .venv/Scripts/python.exe -m pytest tests/unit -q
```

V4 的裁决规则同 `plans/eval-baseline-v1-tooling.md` C4：任何失败都要在 base 干净
worktree 里复现；复现得到 → 记为既有失败并列进证据包，不阻塞；复现不到 → 阻塞。
本机已知既有失败见那份契约的 §Verification commands。

**V3 的裁决规则（实现后、裁决前补登，见文末「修订」）**：V3 在 base 与改动后的树上
各跑一次，两份 concise 输出逐条比较；**改动行新增错误数必须为 0**。base 上已有的
错误记为既有失败、不阻塞——与 V4 同一条归因原则。理由和上一条契约的 B1 相同：
`app/cli.py` 里有两处与本次无关的 `# noqa: BLE001`（非本次改动行），而 Allowed paths
不允许碰方法体之外的代码，门写成「绝对 exit 0」在契约范围内不可能通过。

**解释器路径**：worktree 里没有 `.venv`，四条命令都用主检出的 venv
（`/d/HermesData/moa-gateway/.venv/Scripts/python.exe`），并且 `PYTHONPATH=.`
指向被验证的 worktree，否则会导入主检出的 `app/`。项目不是 editable 安装
（`site-packages` 里没有指向本仓的 `.pth`），所以 `PYTHONPATH` 优先即可。

## Forbidden

- 改 `flow.py` 的异常 → `INFRA_ERROR` 映射（本缺陷的修法是「端口不抛」，不是「放宽映射」）。
- 改 `DeliveryRunState` / `FailureCode` 的定义或语义。
- 让非 TTY 路径返回任何非 `None` 的审批对象（那等于绕过人工门禁）。
- 修改、删除或弱化任何既有测试；加 `xfail`/`skip`。
- 改 `.env`、CI 配置、lint 配置。

## 已知边界（不在本契约内）

- 只修「端口不抛」。若将来有别的审批实现抛异常，`flow.py` 仍会映射成 `infra_error`；
  那是有意保留的行为，不在本次改动范围。
- 退出码：审批被拒后 `delivery run` 以 exit 1 结束（`rejected` 是非零终态），本契约不改。

## 修订（2026-11-16，实现完成、裁决之前）

只补了两条裁决口径，没有改任何验收标准：

1. **V3 的归因规则**（上面已写入 Verification commands）。实现跑 V3 时发现
   `app/cli.py` 有 2 条 RUF100：base `bc323d3` 上是 `168:31` 与 `567:31`，
   改动后是 `168:31` 与 `577:31`（+10 行来自本次方法体），**同两条 `# noqa: BLE001`**，
   都不在改动行。若按「绝对 exit 0」写，契约在 Allowed paths 内不可满足。
   补登的规则与 V4 相同：base 复现得到即记既有失败，但**改动行新增错误必须为 0**。
   照抄上一份契约 B1 的教训：不可赢的门是契约的缺陷，不是实现的缺陷；
   但也不许把门放宽到「看起来绿」——所以换了口径而不是删掉 V3。
2. **解释器路径**：worktree 无 `.venv`，用主检出的 venv + `PYTHONPATH=.`。

实测的 base 证据（裁决前记录，verifier 需亲自复现）：

```
# base bc323d3（干净 worktree .scratch/base-nontty）
ruff --isolated --select F401,F841,RUF100 app/cli.py tests/unit/test_delivery_cli.py
  app\cli.py:168:31: RUF100 ... BLE001
  app\cli.py:567:31: RUF100 ... BLE001
  Found 2 errors
```

## 缺陷的实测证据（base vs 改动后，同一 DEVNULL 条件）

探针：子进程 `stdin=subprocess.DEVNULL`，`asyncio.run(TerminalPushApprovalPort().request(...))`。

```
base   bc323d3:  ISATTY True   →  RAISED EOFError: EOF when reading a line
本改动:          ISATTY True   →  "approver: stdin closed; push approval denied" / RESULT True
```

`ISATTY True` 两行相同，正是缺陷前提：守卫不触发，差别全在 `input()` 之后。
EOFError 一旦抛出，`flow.py` 映射成 `NEEDS_HUMAN` + `INFRA_ERROR`；现在返回 `None`，
走 `flow.py` 的 `REJECTED` + `APPROVAL_REJECTED` 分支。

## 修订 2（2026-11-16，第一轮 verifier PASS 之后，操作者指示收紧测试）

第一轮 verifier 裁决 PASS，同时提出两条非阻塞建议，指出新增测试**弱于它们所声称覆盖的
条款**。操作者指示先收紧再验证。本轮**只改测试断言**，产品代码零改动
（`app/cli.py` 与第一轮裁决的字节相同），验收标准一条未放宽——只是把断言从
「子串」提升到「条款本身」：

1. **criteria 2**（拒绝说明的措辞）：原断言 `"error" not in out.lower()` 抓不到
   `exception`，也不绑定具体拒绝行。现在断言：输出中所有含 `denied` 的行**恰好等于**
   `["stdin closed; push approval denied"]`（一条、原文），且 `error`/`exception`/
   `traceback` 三个词都不出现。两条 EOF 测试都这样断言。
   说明：`"non-interactive stdin; push approval denied"` 这条既有措辞由既有测试
   `test_terminal_approval_denies_non_tty` 覆盖，未改。
2. **criteria 4b**（进程级 DEVNULL 测试）：原实现不断言子进程的 `isatty()`，因此在
   DEVNULL 不算 TTY 的平台上它会走另一条拒绝分支而照样通过——契约声称这条测试
   「钉死平台事实」，实际没有。现在子进程先打印 `ISATTY <bool>`，测试**按实测值分支**：
   `True` → 必须出现 `stdin closed; push approval denied`（本机走的就是这条，即缺陷路径）；
   `False` → 必须出现 `non-interactive stdin; push approval denied`。两平台都断言
   「恰好匹配那一条」，因此任何一种环境下的错配都会失败。
3. **未采纳**：verifier 第 3 条（`sys.stdin.isatty()` 自身可能抛 `ValueError`，如 stdin
   已 close）不在 criteria 1 枚举的三档内，且修复它要动产品代码、需重新裁决产品行为，
   本轮不做。

这一轮不能沿用第一轮的证据：被验证的树变了（新增 commit），V1–V4 必须整轮重跑。
