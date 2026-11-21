# 契约：重复读取不再回灌全文（repeat-read suppression）

- **task id**：`repeat-read-suppress-20261121`
- **分支**：`feat/repeat-read-suppress-20261121`
- **背景**：baseline v2 实测 21 条 live 用例 8 条预算耗尽，全部是 read_file 循环
  （1.2M–1.5M tokens）。现状 `app/agents/stubs.py:388` 对重复的 read_file/search
  只**前置一行标记**（`:404`），完整正文（经 `truncate_observation` 截断后仍可达
  2000 token/条）照发，且留在历史里继续被每次调用重复计费。
- **所有者签收**：用户消息「签收」（2026-11-21，签收 PR #111 后按既定承诺启动本项）。

## 1. 根因与约束

- `MemoryPolicy.max_full_turns = 6`：最近 6 轮完整保留，更早的压成摘要。因此
  **原文是否还在上下文里**是可判定的：轮距 `< max_full_turns` 时还在，`>=` 时已被
  `compact_messages` 折叠进摘要。
- Run Record（`steps[].observation`）必须继续保留**完整观察**——审计证据不缩水，
  压缩只作用于回给模型的消息（与 `memory.py` 模块注释的既有原则一致）。

## 2. 允许路径（Allowed paths）

```
app/agents/stubs.py
app/agents/memory.py
tests/unit/test_structured_tool_loop.py
tests/unit/test_bench_memory.py
plans/repeat-read-suppress-20261121.md
```

## 3. 验收标准

- **S1 窗口内抑制**：同一 `read_file(path, offset, limit)` 第 2 次调用（原文仍在
  上下文窗口内、且本次 outcome 为 ok）→ 回给模型的观察是**重复标记行本身**（含
  `#N` 与"已在上下文中"文案），**不含文件正文**；`steps[].observation` 仍含完整
  观察；`slot["loop_repeat_marks"]` 照常自增。
- **S2 窗口外不抑制**：原文已被 `compact_messages` 折叠（轮距 `>= max_full_turns`）
  时，回给模型的观察仍是标记 + 正文（现行为）——内容确实不在上下文里，重读是合法的。
- **S3 search 同规则**：同一 `(path, pattern)` 的重复 search 适用 S1/S2。
- **S4 失败不抑制**：重复调用 outcome 为 `failed` / `rejected`（error JSON）时，
  正文（错误信息）必须送达，不得抑制。
- **S5 开关**：新增 `MemoryPolicy.suppress_repeated_reads: bool = True`；置 `False`
  时回到现行为（标记 + 正文）。`mark_repeated_reads=False` 时标记与抑制都不发生。
- **S6 第 3+ 次重复**：窗口内第 3、4 次同样抑制，标记 `#3`、`#4` 照常。
- **S7 既有测试**：`test_tool_loop_repeat_marker_on_identical_read` 等 3 条既有
  用例要么继续绿，要么按新语义**等价改写**（断言意图——"标记出现/不出现"——不得
  弱化；禁止删除用例）。

**边界余量**：抑制判据使用轮距 `<= max_full_turns - 1`（预留 1 轮余量，防止本次
append 后 compact 恰好把原文折叠导致模型两头落空）。轮 = 一次 `chat_with_tools`
响应及其全部 tool 消息。

## 4. 验证命令

```
V1  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit/test_structured_tool_loop.py tests/unit/test_bench_memory.py -q
V2  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m ruff check .
```

外加独立探针（Verifier 自写，不入库）：假 llm 脚本化两轮——第 1 轮读同一文件、
第 2 轮重复读同一路径——断言第 2 条 tool 消息是 stub 而非正文；再用
`max_full_turns=1` 复现 S2 的"窗口外仍发正文"。贴脚本与输出。

## 5. 禁止

- 改 `compact_messages` / `truncate_observation` 的语义与签名。
- 抑制波及 read_file/search 之外的工具，或波及 outcome 非 ok 的调用。
- 动 Run Record / 审计口径（`steps[].observation` 必须仍存完整观察）。
- 改/删既有断言换通过；新增 skip/xfail。
- 触碰 Allowed paths 之外的模块（含 `bench/` 与交付管线）。
