# Contract — stdin 不可探测时推送门必须判 rejected，不得抛异常

**冻结于实现之前**（2026-11-16）。上游：PR #102 第二轮 verifier 的非阻塞建议 3
（`TerminalPushApprovalPort` 的 `sys.stdin.isatty()` 自身可能抛 `ValueError`）。

## 缺陷

`app/cli.py` 的 `TerminalPushApprovalPort.request` 现在这样守门（PR #102 合并后的 main）：

```python
if sys.stdin is None or not sys.stdin.isatty():
    print("non-interactive stdin; push approval denied")
    return None
try:
    operator = (await asyncio.to_thread(input, "approver: ")).strip()
    ...
except EOFError:
    print("stdin closed; push approval denied")
    return None
```

`sys.stdin.isatty()` **自己**会抛异常：stdin 已被 `close()` 时抛
`ValueError: I/O operation on closed file`；句柄损坏时可能抛 `OSError`。这一抛发生在
守卫**内部**，不在下面的 `try` 里，于是端口向上抛，`DeliveryFlow` 把它映射成
`NEEDS_HUMAN` + `FailureCode.INFRA_ERROR`（`apps/code_review_pipeline/delivery/flow.py`），
终态又是「基础设施错误」，而不是设计里的 `REJECTED` / `APPROVAL_REJECTED`。

实测（main `5d40319`，`sys.stdin.close()` 后调用端口）：

```
ISATTY_PROBE: RAISED ValueError I/O operation on closed file
PORT RAISED   ValueError I/O operation on closed file
```

与 PR #102 修的是**同一类缺陷**的不同触发点：审批门遇到「无法确认 stdin 可交互」时，
必须 fail-closed（拒绝），不能 fail-loud（抛异常变成 infra_error）。无安全后果
（拒绝就是拒绝），错误同样在终态语义与可诊断性。

## Allowed paths

恰好这三个文件：

```
app/cli.py                                  (只允许改 TerminalPushApprovalPort.request 的方法体)
tests/unit/test_delivery_cli.py             (append only：只能追加测试)
plans/closed-stdin-gate-20261116.md         (本文件，新增)
```

## Acceptance criteria

1. `TerminalPushApprovalPort.request` **从不抛异常**。`sys.stdin` 的探测（`isatty()`）
   抛 `ValueError` 或 `OSError` 时，必须与「非 TTY」同样处理：打印一行拒绝说明、
   返回 `None`。其余既有分支（`sys.stdin is None`、`isatty()` 返回假、提示读取 EOF、
   正常 TTY 路径）行为不得改变。
2. 拒绝说明沿用既有措辞 `non-interactive stdin; push approval denied`（一行、原文）：
   这类 stdin 就是不可交互的，不需要第三种说法；措辞里不得出现 `error`/`exception`/
   `traceback`。
3. **正常路径不变**：`isatty()` 为真、操作者名非空、第二行恰为 `APPROVE` → 返回
   `ChangeApproval`，`run_id/trace_id/bundle_hash` 与入参一致；空操作者名或第二行不是
   `APPROVE` → `None`。既有测试一条都不许改。
4. 新增回归测试（三条）：
   a. `isatty()` 抛 `ValueError` 的伪 stdin → `None` + 精确拒绝行（`denied` 行恰好等于
      `["non-interactive stdin; push approval denied"]`）；
   b. `isatty()` 抛 `OSError` 的伪 stdin → 同上；
   c. 进程级：真子进程先 `sys.stdin.close()` 再调用端口，必须以 0 退出、打印拒绝行、
      **不**出现 `Traceback`/`ValueError`。这条把「close 过的 stdin 在真解释器里是什么样」
      钉死，而不是只靠伪造对象。
5. `tests/unit/test_delivery_cli.py` 只能追加；该文件全部通过。
6. 不新增依赖、不新增 CLI 参数、不引入环境变量开关。
7. **不吞 `KeyboardInterrupt` 之类 BaseException**：只捕 `ValueError` 与 `OSError`。
   审批门可以拒绝，但不许把 Ctrl-C 变成拒绝。

## Verification commands

worktree 里没有 `.venv`，用主检出的解释器，并把 `PYTHONPATH` 指向被验证的 worktree
（项目不是 editable 安装）：

```
V1  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit/test_delivery_cli.py -q
V2  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m ruff check app/cli.py tests/unit/test_delivery_cli.py
V3  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m ruff check --isolated --select F401,F841,RUF100 app/cli.py tests/unit/test_delivery_cli.py
V4  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit -q
```

- **V3 的裁决规则**：在 base `5d40319` 的干净 worktree 里跑同一条命令，用
  `--output-format concise` 逐条比较；**改动行新增错误必须为 0**，base 已有的错误记为
  既有失败、不阻塞。已知 base 上有 2 条 `RUF100`（`app/cli.py` 里两处无关的
  `# noqa: BLE001`），不在改动行。
- **V4 的裁决规则**：任何失败都要在 base `5d40319` 的干净 worktree 里复现；复现得到 →
  既有失败（列进证据包，不阻塞）；复现不到 → 阻塞。

## Forbidden

- 改 `flow.py` 的异常 → `INFRA_ERROR` 映射。
- 改 `DeliveryRunState` / `FailureCode` 的定义或语义。
- 让不可交互的 stdin 路径返回任何非 `None` 的审批对象。
- 捕获 `BaseException` / 裸 `except:`；吞掉 `KeyboardInterrupt`。
- 修改、删除或弱化任何既有测试；新增 `xfail`/`skip`；新增 `# noqa`。
- 改 `.env`、CI 配置、lint 配置、依赖清单。

## 已知边界（不在本契约内）

- 端口在得到 `ChangeApproval` 之后的行为不变；审批通过与推送仍由 `flow.py` 与人负责。
- 若将来还有其它审批实现抛异常，`flow.py` 仍映射成 `infra_error`——那是有意保留的。
