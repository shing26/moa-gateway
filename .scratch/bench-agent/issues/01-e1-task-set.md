# 01 — 任务集：把其余 E1 候选过一遍 fail-first

**What to build:** 剂量曲线要按难度分档，一个任务撑不起一张表。把 §12 里另外四个 E1 候选（GitHub 调用绕过代理、飞书富文本解析、审计链每写者一条、tool_calls 尝试数口径）各自拆成 solution / test 两个 patch 并落成任务定义，然后逐个跑 fail-first：本来就绿、或者修不成一次收敛的候选，连同剔除原因一起如实记下。收工时任务集至少覆盖易 / 中 / 难三档，且每个留下来的任务都有 fail-first 通过的证据。

**Blocked by:** None — can start immediately

**Status:** done

- [x] 四个候选各自产出任务定义与两个 patch（solution / test 分开）
- [x] 每个任务跑 fail-first：通过 fail-first 的留下，不通过的连同原因一起记录
- [x] 留下来的任务覆盖至少三档难度
- [x] 逐任务跑通端到端：scripted agent 写真修复 → `pass`；只改测试 → `fail`
- [x] 既有 988fccb 任务不被改动

**落地：** 四个候选全部通过 fail-first，无一剔除，各落成
`bench/tasks/<sha>.{task.json,solution.patch,test.patch}`：`base_commit` 取修复 commit
的父提交，solution / test 按 `tests/` 路径拆开。fail-first 都是在
`materialize()` 出来的基线上跑钉住的那条判据，退出码 1、失败落在修复面上：

- `a746b81`（易）GitHub 绕过代理：`test_defaults_to_direct_connection` →
  `assert True is False`（httpx `trust_env` 默认 True）。
- `0896629`（易）飞书富文本：`test_post_message_text_is_extracted` →
  `assert None == '...'`（解析器只认 `text`）。
- `bd4f186`（难）审计链每写者一条：`test_two_writers_interleaved_do_not_report_a_break`
  → `AttributeError: app.audit.wal has no attribute '_WRITER_ID'`——修复引入的每写者
  标识在基线不存在，用例据此停在修复面上（其行为断言是"交错写入不判断裂"）。
- `60c64ed`（难）tool_calls 尝试数口径：`test_react_loop_missing_tool_reports_error`
  → `assert 0 == 1`（基线把 `tool_calls` 记成成功数）。该 commit 在同一个大测试文件里
  既改了既有断言又新增了 3 条用例；这里钉的是改动的断言之一，不跑整份文件。

四条 solution patch 都能 `git apply --check` 到 `materialize()` 出来的基线（handoff
条款 5）。顺带补了根 `.gitattributes`（`bench/tasks/*.patch text eol=lf`）：本机
`core.autocrlf=true`，patch 文件若按默认 checkout 成 CRLF，`git apply` 会直接
"patch does not apply"（实测），把已有与新增任务的 patch 一起钉成 LF。难度覆盖：易
`a746b81` / `0896629`、中 `988fccb`（既有）、难 `bd4f186` / `60c64ed`。

端到端回归在 `tests/unit/test_bench_harness.py` 新增三条 parametrized 用例（每个任务
各一参）：fail-first 退出码 1 且失败摘要含钉住的 nodeid；solution 全量写回 → `pass`；
只改测试文件 → 整棵测试树还原后 `fail`。既有 `988fccb` 的 task / patch 零改动。
README 测试基线 1241 → 1253（badge / 摘要 / 离线保障 / 目录树 / 门禁）。
