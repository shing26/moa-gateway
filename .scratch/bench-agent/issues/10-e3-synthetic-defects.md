# 10 — E3 合成缺陷：规格与首批任务

**What to build:** 真实历史 bug 的数量与难度分布是给定的，不是设计的。E3 用合成缺陷把难度变成可控变量（同一个功能面、可调修复面大小）。这一条先定规格——什么样算一个合格的 E3 任务、怎么防止它比 E1 更容易被猜中——然后构造首批并用 fail-first 筛过。

**Blocked by:** 01 — 任务集

**Status:** ready-for-agent

- [x] 写下 E3 任务的合格判据，含 fail-first 与"不可一眼看穿"的检查
- [x] 首批任务落盘并用 fail-first 筛过
- [x] 与 E1 任务在同一份报告里可按来源区分
- [x] 不合格的候选连同剔除原因一并记录
- [x] 本 ticket 不阻塞第一轮曲线（09）

## 落地（2026-10-18）

**规格**：`docs/superpowers/specs/2026-10-18-e3-synthetic-defects.md`（六节）。
合格判据是硬性的五条（fail-first 落在修复面 / 修复收敛 / 只改测试判 fail /
行为级且不可一眼看穿 / **既有测试不因注入变红**）+ LF 与可应用。§12 的"尚未
确定"已改写为该规格的指针。

**任务形态**：E3 与 E1 共用 `bench/tasks/<id>.{task.json,test.patch,solution.patch}`
格式，因为 harness / fail-first / 还原 / 报告都已围着它建好。差别只在语义——
E1 的 `solution` 来自历史修复 commit，E3 的基线是**干净的历史提交**
（`base_commit = 2647e43…` 本 branch 的 HEAD），缺陷由 `test.patch` **注入**到真实
源文件，`solution.patch` 是那条注入的反向。两条 patch 都 LF、都能 `git apply --check`
到 `materialize()` 出来的基线。

**首批三条**（各带 `source: "E3"` 与 `fix_surface`）：

| 任务 id | `fix_surface` | 实测面 | 注入的缺陷 | fail-first 实测特征 |
|---|---|---|---|---|
| `e3-c1-budget-ttl-refresh` | small | 1 行 | `BudgetGuard.record` 不再刷新闲置起点 `last_seen` | `assert 0.0 == 0.6`（持续花钱的会话被误判闲置、累计被清零） |
| `e3-c2-keyword-normalization` | medium | 4 行 | `normalized_keyword_score` 丢掉了"除以池内最高分 + 截断到 1" | `assert 3.0 == 1.0`（强命中文档的分数越过上限） |
| `e3-c3-binary-write-review` | small | 2 行 | `_WRITE_MODES` 只剩文本模式，二进制写模式掉出名单 | `assert False is True`（二进制写被判成干净输出） |

三条都过 `bench/harness/runner.run_task` 的端到端：物化基线 → fail-first 退出码 1
且失败特征命中 → 应用 `solution.patch` → 验证零退出。守卫落在
`tests/unit/test_bench_harness.py` 的 `E3_*` 参数化用例（浅克隆 skip、
逐任务失败特征、fix_surface 声明与 solution 行数对账、scripted agent 真修复 →
`pass` / 只改测试 → `fail`）。

**与 E1 同表可按来源区分**：`TaskSpec.source → RunResult.source`（落盘 JSON 的
`source`）→ `RunRow.task_origin` → `DoseReport.task_sources / origins`。`run_task.py`
的一行摘要与报告的头部都会打印/列出任务来源。既有 5 条 E1 任务本就写着
`"source": "E1"`，零回填。相关判别用例在 `tests/unit/test_bench_report.py`
（`test_report_groups_task_sources_so_e1_and_e3_are_distinguishable` 等）。

**剔除清单（连同原因）**：候选不止三条，被剔的有六类，全部记在这里而非"以后再
说"：

1. **`feishu_signature` 版本优先级倒置**（v2 `header.token` 抢在顶层 `token` 前）
   ——**剔除**：注入后既有 `test_extract_token_prefers_top_level_then_header`
   立刻变红，违反 §2.5。这个模块的契约已被测试钉死，没有留出"注入不惊动既有断言"
   的缝；同类的 v2 token 改动也一并排除。
2. **中文 query 去掉停用字**（`cjk_bigrams` 不再过滤停用 bigram）——**剔除**：
   实测"的"这类停用字在 `query_tokens` 里自成一个字符的 CJK 段，bigram 生成为空，
   缺陷根本不产生可观察差异（fail-first 不成立）。
3. **离线精排把 `token in content` 换成"独立词元相等"**（收得更紧）——**整条剔除**：
   收紧匹配只会让判据更严，无法制造"更差的排序"，fail-first 构造不出可观察的
   退化后果；不是变形留用，是把这一整类候选排除在 E3 手法之外。
4. **`BudgetGuard` 限额边界 `<=` 改 `<`**——**剔除**：既有
   `test_over_limit_session_is_rejected_then_resets` 就在边界上，注入即违反 §2.5。
5. **`context_budget.fit_text` 去掉截断标记的预算扣减**——**剔除**：既有
   `test_fit_text_truncates_and_marks` 直接断言结果在预算内，注入即红。
6. **C1 的初版判据（monkeypatch `time.monotonic`）**——**剔除并重做**：打桩全局时钟会
   连带污染 `_SessionSpend` 的默认工厂时钟，注入之外的既有用例也跟着漂；改用真实
   `time.sleep` 后先遇边界抖动。最终用 `ttl_seconds=0.05` + 两次 `sleep(0.03)`，在
   "距最近一次记账 0.03s、距首次 0.06s"之间留出 ~15ms 裕度，红/绿都确定性复现。

**§2.5 的实测证据**：把三条 E3 缺陷一起注入后跑 `pytest tests/unit`，结果是
`5 failed, 1341 passed, 4 skipped`——红的是且只是三条新判据
（`e3-c1-budget-ttl-refresh` / `e3-c2-keyword-normalization` /
`e3-c3-binary-write-review`）。另两条 `test_rag.py` 失败是 scratch worktree 缺
`./data/` 目录导致的 `sqlite3.OperationalError` 前置失败，与注入无关；同一份
`test_rag.py` 在干净主仓也是同样的两条红（主仓全量跑另有 1359 passed / 4 skipped，
见下）。第一轮筛选时 `_author_e3` 的 C1 行号写偏、误删了 `return entry.total_usd`，
全量跑当场把既有 `test_budget_guard.py::test_default_guard_only_accounts_never_blocks`
照红——修正注入行号后重跑全量即回绿，这条弯路也一并记下（正是 §2.5 该拦的东西）。

**不阻塞 09**：E3 只做到"任务落盘 + fail-first 筛过 + 报告可区分"，没有跑真实
API；09 的第一轮曲线可以只用 E1，E3 是加料不是前置。

**README 同步**：测试基线实测 1359 passed / 4 skipped（badge / 摘要 / 离线保障 /
目录树 / 门禁五处全部写 1359）。改前的 1358 是未实测的旧值，已按本轮全量结果纠正。

## code-review 补记（2026-10-18，fixed point `2647e43`）

对本次 diff 跑了 Standards + Spec 双轴评审，findings 与处置如下。

**Spec 轴**

1. 三条判据最初点名私有函数 / 编码修法，违反规格 §2.4(b) 的"不可一眼看穿" → 三条
   测试文件全部重写为行为级（只看可观察后果：累计是否保留、分数是否可比、写操作
   是否进复核），判据文本不再出现私有函数名或"怎么改"。
2. C1 时序脆弱（monkeypatch 全局时钟 / 真实 sleep 的边界抖动）→ 见剔除清单第 6 条，
   改成留裕度的确定性证据。
3. `fix_surface` 声明与阈值表对不上（初稿 1–5 / 6–20 / 21+，实测 1 / 4 / 2）→ 阈值
   表按实测回填，`large` 一档如实留空。
4. README 计数当时标着"未验证" → 本轮实测回填 1359。
5. E3 fail-first 特征字符串与重写后的判据对不上 → 三条 marker 全部按新判据更新
   （`assert 0.0 == 0.6` / `assert 3.0 == 1.0` / `assert False is True`）。

**Standards 轴**

1. Primitive Obsession（E1 / E3 用裸字符串传来源）→ 新增 `TaskOrigin = Literal["E1","E3"]`
   与 `E1_ORIGIN` / `E3_ORIGIN`，`TaskSpec.source` / `RunResult.source` /
   `RunRow.task_origin` 全走这个闭集；`load_task` 与 `RunRow.from_dict` 对未知来源
   直接 `ValueError`，不再静默落到 E1。
2. 命名不统一（`source` 与 `origin` 混用）→ 报告层的任务来源维度统一叫
   `task_origin`；`source` 一律保留给"哪份 run 文件"这一既有语义。
3. `DoseReport.origins` 只是 `task_source_kinds` 的别名 → 删除别名，`to_dict()`
   直接输出 `task_sources`（任务→来源映射）与 `origins`（出现过的来源种类）。

无 hard violation。六条 patch 全部 `i/lf w/lf`（`git apply --check` 均通过）。
