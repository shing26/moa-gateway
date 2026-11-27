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
  加上 delivery run 中 `state ∈ {ready_for_push_approval, awaiting_merge_approval}` 的记录；
  同一变更按 `run_id` 去重（任一侧缺 `run_id` 时用 `repo#pr@sha` 匹配）。
  merge 记录的时长取 `transitions` 最后一条的 `at`；run 的时长取 `updated_at`；
  取不到时写 `时长未知`。
  **超过 7 天的只计数、不逐条列出**；段标题固定为
  `待你决定: <总数>（可处理 <n> · 超 7 天 <m>）`，可处理项最多列 2 条。
- **卡住了**：delivery run 中 `state ∈ {needs_human, budget_exhausted, failed}` 且
  `updated_at` 在 7 天内。每条带 failure code（无 code 写 `(none)`），**最多列 2 条**，
  其余写进段标题 `卡住了: <总数>（另有 N 条）`。
- **进行中**：**所有未在前两段计数的非终态**。delivery 侧非终态为 preparing /
  implementing / checking / verifier_review / revising / ready_for_push_approval /
  pushing / waiting_ci / awaiting_merge_approval / merging；merge 侧非终态为
  watching / ci_failed / awaiting_approval / merging。终态（delivery 的 merged /
  rejected / needs_human / budget_exhausted / failed，merge 的 merged / rejected /
  failed / superseded）不进任何段。只给总数与 `（delivery N / merge M）` 拆分。
- **近 24h**：`collect_stats(run_dir, since=today - 1 day)` 的 `runs` / `delivered` / `failed`。
- 正文硬上限 10 行，**由每段额度保证而不是先到先得**：固定 5 行（标题 + 三个段标题 +
  近 24h），明细每段最多 2 条；**前一段不得挤占后一段的明细额度**。不得截断 URL。
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

- `triage` 为 `None`（collector 抛异常 / `CiNotFailed` / 无 job）→ 正文与今天完全一致的
  `失败 check 名与链接`，既有断言不改；头部在 `pr_details` 完整时按 D2.6 带
  `PR / 作者 / 链接`，否则保持 `PR / SHA / CI` 三行（v1.4 澄清）。
- `report.actions_unavailable_reason` 非空 → 结论行改为 `结论: 日志不可用（<reason>）`，
  仍列出 `record.ci_failing` 的 check 名与链接。
- 富化失败不得漏发卡片、不得改变状态机结论。

#### D2.4 脱敏与只读

- 所有进入卡片的日志文本在渲染前必须再经一次 `redact_ci_log()`（纵深防御），
  不得直接渲染 `evidence_lines` 原值。
- 分诊只允许调用四个读方法：`get_check_run_status` / `list_workflow_runs_for_sha` /
  `list_workflow_jobs` / `get_job_log`。不得调用任何写方法。
- 成本上限沿用 `CI_TRIAGE_MAX_JOBS` / `CI_TRIAGE_MAX_LOG_CHARS`，不新增配置。

#### D2.5 运行中 workflow 的证据获取（v1.2）

**触发原因**：2026-11-26 19:00 的 `shing26/OpsShield#110` 真实卡片退化为旧三行。
时间线（UTC）：run 创建 10:59:44；三个 job 分别在 10:59:51 / 10:59:52 / 11:00:13 失败；
**11:00:47 发卡**，此时 run 级 `conclusion` 仍为 `null`；run 到 11:00:58 才整体完成。
现有实现只对"run 级结论已是失败值"的 run 拉 job，于是候选为空、0 个 job、永久降级。

- run 候选 = `list_workflow_runs_for_sha(repo, head_sha)` 的结果 **∪** 从失败 check 的 URL
  解析出的 run id，按 run id 去重；候选数上限 `CI_TRIAGE_MAX_JOBS`。
- check URL 形如 `https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>`；
  解析失败、或不是 Actions URL 的 check 跳过，不影响其他 check。
- 解析必须同时校验 **host 与 repo**：仅接受 scheme `https`、host 为 `github.com` 或
  `www.github.com`、且路径中 owner/repo 与目标仓库一致（大小写不敏感）；
  其余 URL 一律跳过。`ci.example.com/actions/runs/789/...` 这类路径形状相同的外部 CI
  不得被当成 GitHub run。
- 对每个候选 run 调用 `list_workflow_jobs(run_id)`，**以 job 级 conclusion 判定失败**；
  不得因为 run 级 `conclusion` 为空 / `in_progress` 而跳过该 run。
- 候选非空但"没有任何失败 job"时，同一轮内最多 **3 次尝试（首试 + 2 次重试）**，
  间隔 10 秒，总阻塞 ≤ 20 秒；仍为空则按 D2.3 降级。候选为空时直接降级，不重试。
- **已取得至少一个失败 job 时，`actions_unavailable_reason` 必须留空**：单个 run 的
  读取失败（404 / 403 / 超时）不得覆盖已收集的证据，也不得让卡片退化为"日志不可用"。
  只有在没有任何失败 job 时才写该原因。
- 等待函数必须可注入，测试不得真睡。
- D2.4 的只读、脱敏、成本上限全部不变。

#### D2.6 卡片可读性（v1.3）

**触发原因**：2026-11-27 送达的首张真实分诊卡（`om_x100b7fb3f45788a4dd5476bb73f6738`）
暴露三个展示缺陷：`结论: unknown` 由多数票产生、信息量最低；正文出现孤立反引号；
卡片没有 PR 标题/作者/链接。

- **结论行**：主类别 = 失败 job 中出现次数最多的**非 `unknown`** 类别；并列时按
  `test_failure > syntax_or_build > dependency > permission_or_auth > timeout >
  cancelled > unknown` 取先者；全部为 `unknown` 时才用 `unknown`。
  渲染 `结论: <主类别> <n> · <次类别> <m> …（<N> 个失败 job）`，最多 3 组。
- **PR 上下文**：CI 失败卡尽量带 `PR: #<n> <title>`、`作者: <login>`、`链接: <html_url>`。
  数据取自现有 `_fetch_pr_details`；失败或字段缺失时退回 `PR: <repo>#<n>`，
  不得漏发卡片、不得阻塞其它卡片。
- **正文渲染**：`ApprovalCard.to_card_payload` 不得再用单个反引号包裹多行输出，
  改为三反引号围栏（或等价）；飞书卡片不得出现孤立 `` ` `` 字符行。
  该修改对所有卡片类型生效，属展示修正，不改变任何按钮/审批语义。

#### D2.7 卡片围栏安全（v1.4）

**触发原因**：v1.3 用固定三反引号包裹正文；正文自带三反引号行时会提前闭合围栏，
其后的 CI 日志被飞书当成 markdown 渲染。

- 写入代码块前，必须把 `agent_output` 中长度 ≥3 的反引号连续串中和：在每个反引号
  之间插入 U+200B（零宽空格），使内部不再出现连续三个反引号。
- 中和必须可逆：去掉 U+200B 后，内容与截断到 2000 字符的原文逐字节一致。
- 围栏仍由首尾两行构成；正文中任何行都不得以 ≥3 个连续反引号开头。
- 对所有 `hitl_kind` 生效；不含连续反引号的正文，卡片 payload 必须与 v1.3 逐字节一致。
- 该修改只影响展示，不改变按钮、审批语义或任何状态机行为。

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
apps/code_review_pipeline/delivery/adapters/github_ci.py        （v1.2）
tests/unit/test_ci_triage_github_adapter.py                     （v1.2）
app/channels/feishu_cards.py                                    （v1.3 / v1.4）
tests/unit/test_feishu_cards.py                                 （v1.3 / v1.4）
tests/unit/test_merge_notifier.py                               （v1.4 仅新增用例）
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
- **D1-A9** 构造 5 条 pending（其中 3 条超过 7 天）与 5 条 stuck：两个明细段各至少列出 1 条，
  `stuck.items` 不得为空，总数与 `hidden` 正确，正文 ≤ 10 行。
- **D1-A10** 真实数据（7 天内有失败单）跑 `--json`：`stuck.items` 非空；
  待你决定段标题含 `可处理` 与 `超 7 天` 两个计数。

### D2 CI 分诊卡

- **D2-A1** 有 triage 时卡片含结论类别、job 数、≤3 条证据行、首个失败 job 链接，且无按钮。
- **D2-A2** 注入 `ghp_` 前缀 token 与 `Authorization: Bearer ...` 形状的日志行，
  卡片文本不含原文（只含脱敏占位）。
- **D2-A3** `triage=None` 时输出与现有失败卡逐字一致（既有测试不改仍绿）。
- **D2-A4** `actions_unavailable_reason` 非空 → 结论标注日志不可用，仍列 check 名与链接。
- **D2-A5** fake github 只记录四个读方法；出现任何写方法调用即测试失败。
- **D2-A6** `list_workflow_runs_for_sha` 返回空，但失败 check 的 URL 指向 run 123、
  且 `list_workflow_jobs(123)` 返回失败 job → 卡片必须含结论与证据，不得降级。
- **D2-A7** run 返回 `{"id": 123, "conclusion": null, "status": "in_progress"}`，
  但 job 级结论为 failure → 必须产出诊断。
- **D2-A8** job 全空的 fake 下：`list_workflow_jobs` 最多调用 3 次，注入的等待函数
  被调用 2 次、每次 10 秒；测试不得真实等待。
- **D2-A9** 非 Actions URL / 空 URL 的 check 不影响其他 check 的解析，且不抛异常。
- **D2-A10** 混合"真实 GitHub run 123 + 伪 run 789（`ci.example.com` 或别的仓库，
  读取时 404）"：job 只来自 123，`actions_unavailable_reason` 为空，
  卡片不得出现"日志不可用"，且 123 的证据行完整。
- **D2-A11** 3 个失败 job（2 个 `unknown` + 1 个 `test_failure`）→ 主类别必须是
  `test_failure`，文本同时含 `unknown 2` 与 `3 个失败 job`；全部 `unknown` 时才显示 `unknown`。
- **D2-A12** `pr_details` 可用 → 卡片含标题、作者、`/pull/` 链接；`get_pr` 抛异常 →
  仍发卡、含 `repo#n`、无标题行、不抛异常。
- **D2-A13** `to_card_payload()` 的 markdown 正文不含以单个反引号开头的行；
  审批/通知/终态三类既有卡片仍可正常渲染。

- **D2-A14** `agent_output` 含三反引号与四反引号行时，卡片 markdown 中以 ≥3 个
  反引号开头的行恰好 2 行（首尾围栏），内部无提前闭合；去掉 U+200B 后正文等于
  原文（2000 字符截断后）。
- **D2-A15** 不含连续反引号的正文，`to_card_payload()` 与 v1.3 逐字节一致；
  审批/通知/终态三类卡片仍正常渲染（D2-A13 不改）。
- **D2-A16** `triage=None` 且 `pr_details` 完整 → 头部含标题、作者、链接，正文
  仍为旧失败 check 列表，不含 `结论:` / `证据:` / `先查:`；`pr_details=None` →
  与旧卡逐字一致；`actions_unavailable_reason` 非空时同样允许 PR 头。

### D3 通用

- **D3-A1** 摘要或分诊抛异常时，同一轮的其他审批卡/失败卡照发，worker 不退出（既有 worker 测试全绿）。
- **D3-A2** `ruff check .` 全绿；不新增依赖；不删或弱化任何既有断言。
- **D3-A3** 审批身份校验、`bundle_hash` 绑定、head-SHA 乐观锁、状态机转移表零改动。
- **D3-A4** 既有 D2-A1…A5 与降级测试语义不变且仍绿；分诊仍无任何 GitHub 写调用。

## 4. 验证命令

```
V1  最窄相关
    $env:PYTHONPATH='.'; .venv\Scripts\python.exe -m pytest `
      tests/unit/test_digest.py tests/unit/test_merge_notifier.py `
      tests/unit/test_merge_worker.py tests/unit/test_ci_triage.py `
      tests/unit/test_merge_poller.py tests/unit/test_feishu_cards.py -q

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
- 用删除或替换内容的方式规避围栏（必须可逆中和，且不得改动日志语义）。
- 改动 D2.6 的结论排序或 PR 头字段规则。
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

**v1.1 修订**：所有者书面批准「批准 v1.1 修订」（2026-11-26）。修订内容：待你决定段引入
7 天陈旧项折叠与每段明细额度；进行中段改为"所有未在前两段计数的非终态"；新增
D1-A9 / D1-A10。触发原因：真实数据下 v1.0 的行预算被陈旧待批挤占，
`stuck.items` 为空——合规但不可用。

**v1.2 修订**：所有者书面批准「批准 v1.2 修订」（2026-11-26）。修订内容：D2.5
运行中 workflow 的证据获取（check URL 反解 run id、以 job 级结论为准、最多 3 次尝试）；
新增 D2-A6…A9 与 D3-A4。触发原因：`shing26/OpsShield#110` 的真实 CI 红卡退化为旧三行，
根因是 run 级结论晚于 check 级失败，而富化只做一次。

**v1.2 澄清**（2026-11-27，Verifier 首轮 REJECT 驱动）：解析 check URL 必须校验 host 与
repo；已取得失败 job 时不得用单个 run 的读取失败覆盖证据。新增 D2-A10。
该澄清是 D2.5"不影响其他 check"的直接展开，不扩大范围。

**v1.3 修订**：所有者书面批准「批准 v1.3 修订」（2026-11-27）。修订内容：D2.6
卡片可读性（结论分类拆分、PR 标题/作者/链接、去掉裸反引号）；新增 D2-A11…A13。

**实现**：独立会话在 `feat/governance-digest-20261126` 分支完成，不得与本会话兼任裁决。

**验收**：独立 Verifier 重跑 V1–V3，`ACCEPT` 后由所有者签收。

**待验（合并后补齐）**：一张真实摘要卡、一张真实 CI 红分诊卡的飞书截图或消息 id。

**所有者签收**：2026-11-26，用户消息「签收 governance-digest-20261126」。

**合并**：PR #150 → `main`，merge commit
`faa423911555d0f4d2c89ad7d5f7404f704fadc7`。

**验收证据**：独立 Verifier 最终 `ACCEPT`（合并树 HEAD `f71a83d`，父
`1d3b953` + `78ee40d`）。D1-A1…A10、D2-A1…A5、D3-A1…A3 全 PASS，P0/P1 无。
V1 `87 passed`；V2 `2427 passed, 6 skipped`；V3 `All checks passed!`；
V4 真实数据对账：待你决定 29（可处理 1 · 超 7 天 28）、卡住了 11（列出 2 条）、
进行中 35，与 `delivery stats` 复算一致。GitHub CI：`test` / `integration` 绿，
`docker` skipped。

**两轮打回记录**：

- 首轮 `REJECT`（HEAD `edc8280`）：`merge.ci_failed` 与 `delivery.merging` 非终态漏计 →
  `9d458e6` 修复。
- 契约 v1.1（`791280c`，所有者批准）：陈旧待批挤占行预算，`stuck.items` 为空 →
  `1d3b953` 修复。

**冲突解决**：PR #150 与 #148/#149 冲突于 `tests/unit/test_merge_worker.py`；
取 `main` 版本 + 追加本单两个测试，双方均无删除行，合并树复核后 `ACCEPT`。

**运行时确认**：2026-11-26 14:13（+08），主工作区已快进到 `cd39dfd`，8083 网关
继续运行，merge worker 单独重启到该提交（心跳与 `/healthz` 均为 `running`）。
重启后首轮即发出第一张真实摘要卡：message id
`om_x100b7f48ae8fa0b4c0c0eb15c813035`；`data/digest/state.json` 记录
`last_sent_date=2026-11-26`、`message_id` 同上。

**已完成（原待验项）**：真实 CI 红分诊卡已于 2026-11-27 02:35 送达
（`shing26/OpsShield#110`，message id `om_x100b7fb3f45788a4dd5476bb73f6738`）。

**v1.2 合并**：PR #156 → `main`，merge commit
`e9aa64b73afeab0e8879d49d512f803662206f20`（分支 `fix/ci-triage-running-run-20261126`）。
验收 HEAD `ea5559a`；合并 `main` 文档提交后为 `7b6715d`，代码相对验收 HEAD 零 diff。

**v1.2 验收证据**：独立 Verifier `ACCEPT`。V1 `98 passed`；V2 `2437 passed, 6 skipped`
（可用内存 3.6 GB）；V3 `All checks passed!`。D2-A6…A10、D3-A4 全 PASS，P0/P1 无。
首轮 `REJECT` 的 P1（`ci.example.com/actions/runs/789` 被误判为 GitHub run 并覆盖证据）
已由 host/repo 校验 + "有证据时清空 limitation" 关闭，复现输出 `[]`。
真实只读探针 `shing26/OpsShield#110`：3 个 job，`test_failure` / `unknown` / `unknown`。

**v1.2 运行时**：2026-11-27 01:26，merge worker 已重启到 `e9aa64b`。用生产渲染路径
对 `shing26/OpsShield#110` 生成了新格式卡片（`结论: unknown（3 个失败 job）` +
3 条证据 + 先查链接 + 只读说明）。当时本机网络与 Docker 均异常：飞书出站
`httpx.ConnectError`，Postgres `:5433` 不可达，故这张卡未送达。

**v1.2 送达**：2026-11-27 02:35，Postgres/Redis 容器恢复、merge worker 自愈后，
对同一记录重放该卡，飞书返回 message id `om_x100b7fb3f45788a4dd5476bb73f6738`。
真实 CI 红分诊卡至此完成端到端送达；后续真实失败将自动走同一路径。

**v1.3 合并**：PR #161 → `main`，merge commit
`38043bc41f3688d1c07717f8dee29cf199b061c7`（分支 `fix/ci-card-display-20261127`，
验收 HEAD `3fe899b1ff42bff04a23a1fa8f05580335aba228`）。

**所有者签收**：2026-11-27，用户消息「批准 v1.3 修订」。

**v1.3 验收证据**：独立 Verifier `ACCEPT`（HEAD `3fe899b`），D2-A11…A13 全 PASS，
P0/P1 无。签收前复跑：V1（契约 5 文件）`92 passed`（含 v1.3 新增文件的 7 文件集合
`121 passed`）；V2 `2445 passed, 6 skipped`；V3 `All checks passed!`。

**v1.3 运行时**：2026-11-27 03:58（+08），主工作区代码到 `38043bc`，merge worker
重启后 `/healthz` 为 `healthy`、`merge_worker: running`。用生产渲染路径对
`shing26/OpsShield#110@4068de2ebb66f281daecc2498104747c3c50939c` 重新渲染，
三处修复均生效：

- 结论行 `结论: test_failure 1 · unknown 2（3 个失败 job）`，不再由 2 个 `unknown`
  的多数票决定主类别。
- 卡片头含 `PR  : #110 docs(readme): sync Python probes badge with CI`、
  `作者: shing26`、`链接: https://github.com/shing26/OpsShield/pull/110`。
- 正文为三反引号围栏，无孤立单反引号行；证据含 3 条断言输出。

该次为本地渲染验证，未重复发送；该 PR 的送达记录仍是原卡
`om_x100b7fb3f45788a4dd5476bb73f6738`。

**v1.3 遗留观察（P2，不阻塞）**：

- 正文自身带三反引号围栏时可能提前闭合卡片围栏；D2-A13 目前只覆盖单反引号行。
- `triage=None` 且 `pr_details` 非空时实际输出富化头，与 D2.3「与今天完全一致」
  的字面不符；该组合无测试覆盖。

**v1.4 修订**：所有者书面批准「批准 v1.4 修订」（2026-11-27）。修订内容：新增
D2.7 卡片围栏安全；澄清 D2.3 降级路径允许 PR 上下文；新增 D2-A14…A16。
触发原因：v1.3 遗留两项 P2——正文自带三反引号会提前闭合围栏；`triage=None` +
`pr_details` 非空时头部与 D2.3 字面不符。实现与裁决分离，由独立 Implementer
在独立分支完成，独立 Verifier 重跑后签收。
