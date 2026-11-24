# 契约：preflight 的 verify 探测去 Python 化

- **task id**：`delivery-preflight-verify-multilang-20261124`
- **分支**：`fix/preflight-verify-multilang-20261124`
- **背景**：第一次跨仓交付（`shing26/OpsShield`，Java/Maven）被
  `delivery preflight` 直接 blocked：

  ```
  blocked verify '...\mvn.CMD' 退出码 1:
  [ERROR] The goal you specified requires a project to execute but there is no POM in this directory
  ```

  根因：`_check_verify` 把 `argv[0]` 当成 Python 解释器，固定用
  `[argv[0], "-c", "import sys; print(sys.version)"]` 探测。任何非 Python 的
  verify 命令（mvn / gradle / npm / go）都会被判 blocked，于是
  `delivery run` 永远进不去。这正是「跨仓复用是假设不是证据」的实证。

## 1. 语义

1. **Python 形状不变**：当 `argv[0]` 的 basename（去掉 `.exe`、大小写不敏感）
   属于 `{python, python3, py}` 时，继续用
   `["-c", "import sys; print(sys.version)"]` 探测——它额外证明了 venv 里
   能 import sys，比 `--version` 强。
2. **其他程序**：用 `["--version"]` 探测；退出码 0 视为 ok，非 0 视为 blocked
   并在 detail 里带程序名与输出摘要。
3. **失败形状不变**：程序解析不到 / 启动抛 `OSError` / 超时 → blocked，
   detail 文案保持现有风格，不得抛异常。
4. 探测仍在**空临时目录**里跑，不克隆、不联网、不写仓库文件。

## 2. 允许路径

```
apps/code_review_pipeline/delivery/preflight.py
tests/unit/test_delivery_preflight.py
plans/delivery-preflight-verify-multilang-20261124.md
```

## 3. 验收标准

- **T1** `python` / `python3` / `py` / `PYTHON.EXE` 走 `-c "import sys"` 分支
  （可用 spy 断言 argv 形状），既有 `test_verify_bare_python_ok` 不改且通过。
- **T2** 非 Python 程序（用桩可执行文件或 `sys.executable` 之外的真实程序）
  走 `--version` 分支；exit 0 → `STATUS_OK`，detail 含程序名；
  exit 非 0 → `STATUS_BLOCKED` 且 detail 含输出摘要。
- **T3** 相对路径不存在（既有用例）、超时、空命令三条 blocked 路径行为不变。
- **T4** 不新增依赖；不删弱化既有断言；`ruff check .` 全绿。
- **T5** 真实冒烟：`delivery preflight --repo shing26/OpsShield` 的 `verify`
  项从 blocked 变 ok（由主会话在合并后跑，作为运维证据）。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; python -m pytest tests/unit/test_delivery_preflight.py tests/unit/test_delivery_cli.py -q
V2  $env:PYTHONPATH='.'; python -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; python -m ruff check .
```

解释器一律用 `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`。

## 5. 禁止

- 放宽「程序不存在 / 超时」的 blocked 判定（fail-closed 不能退）。
- 让 Python 分支退化成 `--version`（会丢掉 venv 可用性证据）。
- 在探测里执行仓库代码、联网或写文件。
- 新增依赖；改动 Allowed paths 之外的文件。

## 6. 签收记录

**所有者书面指令**：2026-11-24 消息「先收再改卡片」——先收口本单，再做合并
审批卡的信息设计改造。

**验收证据**（独立 Verifier，HEAD `884d664`）：

- V1 `61 passed`；V2 `2349 passed, 6 skipped`；V3 `All checks passed!`
- T1–T4 全 PASS；探测仍在空临时目录、fail-closed 未放宽
- 真实冒烟：`delivery preflight --repo shing26/OpsShield` 的 `verify` 项
  `blocked → ok`，detail 为 `程序 …\mvn.CMD 可启动`；
  旧形状 `mvn -c "import sys"` 复现 exit 1，新形状 `mvn --version` exit 0

**合并**：PR #137 → `main`，merge commit
`fda9be40fa6b1cccba5ba8b7b1cb0aefdad47067`。
