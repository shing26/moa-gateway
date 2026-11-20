# 契约：preflight verify 检查的 P1 修复（PR #111）

- **task id**：`preflight-verify-fix-20261120`
- **分支**：`feat/preflight-verify-command-delivery-1795077962348775500`（PR #111 的 head）
- **触发**：独立 Verifier 对 PR #111 判定 **不可合并**，列出 3 个 P1。
- **所有者签收**：用户消息「转下方向先把自用做好」（授权按自用优先继续推进）。

## 1. 待修问题（Verifier 实测，有输出）

1. **P1 超时未映射为 blocked**：探测子进程超时抛 `subprocess.TimeoutExpired`，
   其 MRO 是 `SubprocessError -> Exception`，**不是 `OSError`**，现有 `except OSError`
   捕获不到，异常直接冒泡。
2. **P1 空 verify 命令导致崩溃**：无档案且无环境变量时 `resolve_delivery_config`
   返回 `()`（`tests/unit/test_repo_profile.py:220` 明确断言），CLI 原样传入后
   `_check_verify` 在 `preflight.py:213` 对 `argv[0]` 取值 → `IndexError`。
   这破坏了“既有调用方行为不变”。
3. **P1 验收测试缺失**：没有任何测试断言 `delivery preflight --json` 输出里出现
   `verify` 条目。

## 2. 允许路径（Allowed paths）

```
apps/code_review_pipeline/delivery/preflight.py
app/cli.py
tests/unit/test_delivery_preflight.py
tests/unit/test_repo_profile.py
plans/preflight-verify-fix-20261120.md
```

## 3. 验收标准

- **A1 超时**：探测超时 → `verify` 项为 `blocked`，detail 说明超时与秒数；异常不得冒泡。
- **A2 空命令**：`verify_command == ()` → `verify` 项为 `blocked`，detail 明确“未配置/为空”，
  不得抛异常；`verify_command is None` → 不产生 `verify` 项（既有行为不变）。
- **A3 JSON**：新增测试断言 `cmd_delivery_preflight(..., json=True)` 输出含 `verify`
  条目且带 `status` / `detail`；既有人类可读格式不变。
- **A4 既有语义不变**：坏相对路径 `.venv/Scripts/python.exe` → `blocked`；裸 `python` → `ok`；
  五项既有检查的名称、顺序、语义不变；`build_verify_env` / `resolve_program` /
  `run_command` 不改。
- **A5 硬规则**：不得给具名异常加 `# noqa: BLE001`（会触发 RUF100）；不得新增 skip/xfail；
  不得改 `config/repos/**`。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit/test_delivery_preflight.py tests/unit/test_repo_profile.py -q
V2  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m ruff check .
```

外加独立探针：分别用 `()`、`None`、坏路径、裸 `python`、注入超时四种输入调
`run_preflight`，证明四种结果分别是 blocked / 无该项 / blocked / ok / blocked。

## 5. 禁止

- 把超时或空命令“静默忽略”（那会让坏档案重新变成非得烧完一整单才暴露）。
- 扩大改动到 `preflight.py` 之外的非测试代码（`app/cli.py` 仅限
  `cmd_delivery_preflight` 及其参数传递）。
- 改既有断言换通过。
