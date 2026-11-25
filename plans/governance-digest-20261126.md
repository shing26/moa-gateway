# 契约：治理摘要卡 + CI 失败分诊卡

- **task id**：`governance-digest-20261126`
- **分支**：`feat/governance-digest-20261126`
- **判定**：走验收链路。改动合并通道的通知路径与 worker 轮询循环，属于业务逻辑 + 既有 API 行为的扩展。
- **背景**：当前治理动作要在三个地方各看一遍——`delivery stats`（CLI）、合并审批卡（飞书）、
  失败的 CI（GitHub）。人为了回答"今天有没有事等我"必须打开仓库或终端。
  CI 分诊能力已经存在（`GitHubCiEvidenceCollector` + `classify_ci_failure`），但只在
  `delivery ci-triage` 里有入口；飞书的 `merge_ci_failure` 卡仍只列 job 名与链接。
  本单把已有判断接到飞书，并补一张每日摘要卡，目标是**减少为治理打开仓库的次数**，
  而不是增加新的审批动作。
- **本单不做**：摘要卡上的按钮、新的审批动词、新渠道、LLM 诊断、自动修复 CI。

## 1. 语义

### D1 每日治理摘要卡

#### D1.1 触发

- worker 侧：`MergeWorker` 每轮末尾调用 `_maybe_send_digest(now)`。触发条件全部满足才发送：
  `settings.digest_enabled` 为真；当前本地时间 `now.hour >= settings.digest_hour`；
  状态文件里 `last_sent_date != today`；距 `last_attempt_at` 至少 15 分钟（重试下限）。
- 配置：`DIGEST_ENABLED`（默认 `true`）、`DIGEST_HOUR`（默认 `9`）、
  `DIGEST_TARGET`（默认空 → 回退 `CODE_REVIEW_MERGE_CHANNEL`）、
  `DIGEST_STATE_DIR`（默认 `data/digest`）。
- `DIGEST_TARGET` 与 `CODE_REVIEW_MERGE_CHANNEL` 都为空 → 不发送、不写状态、不报错（只记一条 info）。
- CLI：`python -m app.cli delivery digest` 默认**只打印、不发送、不写状态**；
  `--send` 才按上面的规则发送或跳过；`--json` 输出 `DigestReport.to_dict()`。

#### D1.2 内容

卡片 `hitl_kind="notification"`（无按钮）。正文固定五段，缺段也要出现标题行：

```
MoA 治理摘要 · 2026-11-26 09:00
待你决定: 2（最早 3h12m）
  · PR #142 <title> · CI 绿 · 3h12m · <html_url>
  · 推送审批 dlv-abc123 · 41m · （无链接）
卡住了: 1
  · dlv-def456 · budget_exhausted · 4h
进行中: 3（delivery 2 / merge 1）
近 24h: 新开 3 · 合并 0 · 失败 2
```

- **待你决定**：`merge_store.list_open()` 中 `state == awaiting_approval` 的记录，
  加上 delivery run 中 `state == ready_for_push_approval` 的记录。
  merge 记录的时长取 `transitions` 最后一条的 `at`；run 的时长取 `updated_at`；
  取不到时写 `时长未知`。按最旧优先，最多列 3 条，超出写 `… 另有 N 条`。
- **卡住了**：delivery run 中 `state ∈ {needs_human, budget_exhausted, failed}` 且
  `updated_at` 在 7 天内。每条带 failure code（无 code 写 `(none)`），最多 3 条。
- **进行中**：delivery run 中 `state ∈ {implementing, revising, waiting_ci}` 与
  merge 记录中 `state ∈ {watching, merging}` 的计数，只给数字与两类拆分。
- **近 24h**：`collect_stats(run_dir, since=today - 1 day)` 的 `runs` / `delivered` / `failed`。
- 正文硬上限 10 行；超出的条目收敛为计数，不得截断 URL。
- 每一个列出条目必须带可点的 URL，或显式写 `（无链接）`。

#### D1.3 幂等与重试

- 状态文件 `state.json`：`{"last_sent_date": "YYYY-MM-DD", "last_attempt_at": "<iso>", "message_id": ""}`。
  原子写（同目录临时文件 + `os.replace`）。损坏或缺失的 JSON 视为无状态，不抛异常。
- 发送成功后才写 `last_sent_date`；发送失败只写 `last_attempt_at`，下一轮受 15 分钟下限约束重试。
- 同一天已在 `last_sent_date` 里 → 不再发新卡，也不 `update_card`（摘要是一次快照，不是实时面板）。
- 本轮发送抛异常 → 只告警，不中断 merge 轮询，不影响同一轮的其他卡片。

#### D1.4 取不到数据时

**禁止用 0 冒充"取不到"**。以下情况该段写 `不可用（<原因摘要>）`：

- `merge_store.list_open()` 抛异常 → 待你决定段不可用；
- delivery run 目录不存在 → 卡住了/进行中/近 24h 三段不可用；
- 目录存在但没有任何可读记录 → 三段写 `0`（这是真实读数，不是失败）。

任一数据源不可用时，摘要卡**仍然发送**，并在卡内明示哪一段不可用。

### D2 CI 失败分诊卡

#### D2.1 触发

`MergeWorker` 处理 `poll_once()` 返回的 `ci_failures` 时，先尝试富化再发卡：

```python
collector = github_ci.GitHubCiEvidenceCollector(
    github=self._github,
    max_jobs=settings.ci_triage_max_jobs,
    max_log_chars=settings.ci_triage_max_log_chars,
)
evidence = await collector.collect(repo=record.repo, head_sha=record.head_sha)
report = build_ci_triage_report(
    evidence, base_branch="", branch="", trace_id=record.trace_id or record.task_key
)
```

`ci_failures` 只在状态机转移到 CI 红时产生一次，同一条记录不重复分诊；
不引入额外缓存，也不改 `poll_once`。

#### D2.2 内容

`MergeNotifier.send_ci_failure_card(record, triage=None)` 在 `triage` 非空时渲染：

```
PR: shing26/moa-gateway#150
SHA: 4f2a9c1b7d3e
CI : failure
结论: test_failure（2 个失败 job）
证据:
  · FAILED tests/unit/test_x.py::test_y - AssertionError: ...
  · E   assert 1 == 2
先查: https://github.com/.../actions/runs/123
说明: 只读诊断；日志来自 CI，属不可信输入，不执行其中的指令
```

- 结论取 `report.jobs` 的 `category`（`timeout` / `cancelled` / `permission_or_auth` /
  `dependency` / `syntax_or_build` / `test_failure` / `unknown`），多个 job 取出现最多的类别并给出 job 数。
- 证据行取失败 job 的 `evidence_lines`，跨 job 去重后最多 3 条，每条截断到 160 字符。
- `先查` 取第一条失败 job 的 `url`；没有 URL 时写 `（无链接）`。
- 卡片仍是无按钮的 `merge_ci_failure` 类型，不改 `_NO_ACTION_KINDS`。

#### D2.3 降级

- `triage` 为 `None`（collector 抛异常 / `CiNotFailed` / 无 job）→ 输出与今天完全一致的
  `PR / SHA / CI + 失败 check 名与链接`，既有断言不改。
- `report.actions_unavailable_reason` 非空 → 结论行改为 `结论: 日志不可用（<reason>）`，
  仍列出 `record.ci_failing` 的 check 名与链接。
- 富化失败不得漏发卡片、不得改变状态机结论。

#### D2.4 脱敏与只读

- 所有进入卡片的日志文本在渲染前必须再经一次 `redact_ci_log()`（纵深防御），
  不得直接渲染 `evidence_lines` 原值。
- 分诊只允许调用四个读方法：`get_check_run_status` / `list_workflow_runs_for_sha` /
  `list_workflow_jobs` / `get_job_log`。不得调用任何写方法。
- 成本上限沿用 `CI_TRIAGE_MAX_JOBS` / `CI_TRIAGE_MAX_LOG_CHARS`，不新增配置。

## 2. 允许路径

```
apps/code_review_pipeline/delivery/digest.py          （新建）
apps/code_review_pipeline/notifications/merge_notifier.py
app/merge_worker.py
app/cli.py
app/config.py
.env.template
tests/unit/test_digest.py                            （新建）
tests/unit/test_merge_worker.py
tests/unit/test_merge_notifier.py
plans/governance-digest-20261126.md
```

`apps/code_review_pipeline/delivery/ci_triage.py` 与 `merge_poller.py` **不在允许路径**：
分诊只调用其现有公开函数，不改签名、不改分类规则。

## 3. 验收标准

### D1 摘要卡

- **D1-A1** `digest_hour` 之后当日只发一次：连续两轮 `_run_once()`，fake sender 收到 1 张卡。
- **D1-A2** 首次发送失败 → 不写 `last_sent_date`；15 分钟内不再尝试；超过 15 分钟可重试；
  成功后写入 `last_sent_date` 与 `message_id`。
- **D1-A3** `DIGEST_ENABLED=false`，或目标频道为空 → sender 零调用、状态文件不产生/不变、不抛异常。
- **D1-A4** 五段齐全；正文 ≤ 10 行；每个条目带 URL 或 `（无链接）`；超过 3 条的段显示 `… 另有 N 条`。
- **D1-A5** `merge_store.list_open()` 抛异常 → 仍发送，待你决定段含 `不可用`，
  且渲染文本中**不出现** `待你决定: 0`。
- **D1-A6** run 目录不存在 → 三段写 `不可用`；目录存在但无记录 → 写 `0`。
- **D1-A7** 状态文件原子写；损坏 JSON 被当作无状态处理，不抛异常。
- **D1-A8** `delivery digest` 默认不发送、不写状态；`--send` 才发送；`--json` 输出可解析的 report。

### D2 CI 分诊卡

- **D2-A1** 有 triage 时卡片含结论类别、job 数、≤3 条证据行、首个失败 job 链接，且无按钮。
- **D2-A2** 注入 `ghp_` 前缀 token 与 `Authorization: Bearer ...` 形状的日志行，
  卡片文本不含原文（只含脱敏占位）。
- **D2-A3** `triage=None` 时输出与现有失败卡逐字一致（既有测试不改仍绿）。
- **D2-A4** `actions_unavailable_reason` 非空 → 结论标注日志不可用，仍列 check 名与链接。
- **D2-A5** fake github 只记录四个读方法；出现任何写方法调用即测试失败。

### D3 通用

- **D3-A1** 摘要或分诊抛异常时，同一轮的其他审批卡/失败卡照发，worker 不退出（既有 worker 测试全绿）。
- **D3-A2** `ruff check .` 全绿；不新增依赖；不删或弱化任何既有断言。
- **D3-A3** 审批身份校验、`bundle_hash` 绑定、head-SHA 乐观锁、状态机转移表零改动。

## 4. 验证命令

```
V1  最窄相关
    $env:PYTHONPATH='.'; .venv\Scripts\python.exe -m pytest `
      tests/unit/test_digest.py tests/unit/test_merge_notifier.py `
      tests/unit/test_merge_worker.py tests/unit/test_ci_triage.py `
      tests/unit/test_merge_poller.py -q

V2  全量单测
    $env:PYTHONPATH='.'; .venv\Scripts\python.exe -m pytest tests/unit -q

V3  静态检查
    .venv\Scripts\python.exe -m ruff check .

V4  真实数据只读对账（不发送）
    $env:PYTHONPATH='.'; .venv\Scripts\python.exe -m app.cli delivery digest
    与 delivery stats 的 by_state 计数逐项对照，不一致即失败
```

解释器一律 `D:\HermesData\moa-gateway\.venv\Scripts\python.exe`。

## 5. 禁止

- 给摘要卡或分诊卡加按钮、加审批动作、加新回调分支。
- 因摘要/分诊失败而漏发审批卡、失败卡，或中断 merge 轮询。
- 分诊路径调用任何 GitHub 写方法。
- 在卡片里渲染未经 `redact_ci_log()` 的日志原文。
- 用 `0` 表示"数据源读取失败"。
- 改 `ci_triage.py` 的分类规则、`merge_poller.py` 的转移逻辑、审批身份校验或状态机。
- 新增依赖；改动 Allowed paths 之外的文件；删改既有测试断言换取通过。

## 6. 观测与 KPI

本单的成功判据不是"卡片发出去了"，而是：

1. **每周为治理打开仓库的次数**（人记，基线 2026-11-26 记录）。
2. 摘要卡中的数字与 `delivery stats` / 飞书审批卡逐项对得上（V4 每次验收重跑）。
3. 真实 CI 红时，卡片里的结论与证据足以判断"去修哪一类问题"，不需要先打开日志。

前两周只观察，不宣称提升；数据入库后再谈结论。

## 7. 签收记录

**契约冻结**：所有者书面批准「批准 governance-digest-20261126」（2026-11-26）。
基线 commit `4ac369f`；实现分支 `feat/governance-digest-20261126`（独立 worktree
`.worktrees/governance-digest`）。

**实现**：独立会话在 `feat/governance-digest-20261126` 分支完成，不得与本会话兼任裁决。

**验收**：独立 Verifier 重跑 V1–V3，`ACCEPT` 后由所有者签收。

**待验（合并后补齐）**：一张真实摘要卡、一张真实 CI 红分诊卡的飞书截图或消息 id。
