# Contract — PR #71 合并前整改：脱敏覆盖与 ADR 口径对齐

**冻结于整改之前**（2026-11-17）。对象：PR
shing26/moa-gateway#71（`feat/output-redaction`，head `17c5289`），
其独立评审结论：**建议合并**，但合并前必须处理 B2（ADR 承诺宽于正则实际覆盖）
与 B3（ADR 事实性表述），并完成 B1（分支更新 + CI + 本契约）。

## 背景

PR #71 把输出侧脱敏接到两个真实出口（`AuditEntry.to_audit_dict()` 的 WAL/ES 序列化、
`change/pipeline.py` 的建 commit / 开 PR），独立评审已确认：确定性（跨
`PYTHONHASHSEED` 一致）、对普通审计文本保守（15 条样例零误杀）、测试有效
（精确输出断言，且经真实 `publish_bundle()` 路径验证）。

评审同时实测到漏杀：

```
Authorization: Basic <base64>          → 原样返回
Authorization: token <opaque>          → 原样返回
Authorization: Digest ...              → 原样返回
DB_PASSWORD=<12+ chars>                → 原样返回
{"password": "<12+ chars>"}            → 原样返回
```

原因：`auth-header` 不认 scheme；`key-value` 的 `\b` 锚定使 `DB_PASSWORD` 这类
前缀变量名不命中；JSON 形态因字段名后先有引号而不命中。另有小瑕疵：
`password = "..."` 脱敏后吃掉收尾引号。

## Allowed paths

恰好这些文件（在 PR 分支上改动，不开新分支之外的文件）：

```
app/redact.py                                   (扩充形态 + 修收尾引号)
docs/adr/adr-035-output-redaction.md            (B3：口径与事实对齐，不扩大承诺)
tests/unit/test_redact.py                       (对每个新增形态补精确断言；append 为主)
tests/unit/test_change_pipeline.py              (仅在需要时补一条出口用例)
plans/pr71-redaction-remediation-20261117.md    (本文件，新增)
```

## Acceptance criteria

1. **五类漏杀必须被捕获**，且各自有精确输出断言（不是 `!=` 弱断言）：
   a. `Authorization: Basic <base64>` → 凭证替换，scheme 保留；
   b. `Authorization: token <opaque>` → 同上；
   c. `Authorization: Digest ...` → 同上；
   d. `DB_PASSWORD=<值>`、`GITHUB_TOKEN=<值>` 这类**带前缀的变量名** → 值替换；
   e. `{"password": "<值>"}` 这类 JSON 形态 → 值替换。
2. **保守性不得退化**：评审用的 15 条普通审计文本（`Authorization: missing`、
   `token: rotated at 12:30`、`Bearer of bad news: retry scheduled` 等）必须仍原样通过。
   新增形态不得引入新的误杀；测试里必须留这些反例。
3. `password = "..."` 脱敏后收尾引号保留（可读性缺陷修复）。
4. **确定性不变**：同一输入同一输出；`test_deterministic` 仍通过。
5. **ADR-035 事实修正（B3）**：删掉/改写两处不成立的说法——
   “此前只有输入侧的 `subprocess_env`”（`ci_triage.redact_ci_log` 自 S0 就存在）、
   “形态清单集中在 `_PATTERNS` 一处”（仓库内是两套正则、两种占位符）。
   若不同步合并 CI triage 的清单，必须在 ADR 里写明这是已知边界与后续方向。
6. **不扩大范围**：不合并 `ci_triage` 的正则；不改两个出口的接线方式；
   不改 `app/audit/**`、`change/pipeline.py` 的既有语义；不新增依赖。
7. 既有测试全绿；本 PR 触及的测试文件精确通过：
   `pytest tests/unit/test_redact.py tests/unit/test_change_pipeline.py -q`。
8. **B1 流程**：整改 commit 落在 PR 分支上；分支更新到当前 `main` 后 CI `test` 必须绿；
   合并在书面签收后进行。

## Verification commands

用主检出的解释器，`PYTHONPATH` 指向被验证的 worktree（项目不是 editable 安装）：

```
V1  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit/test_redact.py tests/unit/test_change_pipeline.py -q
V2  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m ruff check app/redact.py tests/unit/test_redact.py docs/adr/adr-035-output-redaction.md
V3  PYTHONPATH=. /d/HermesData/moa-gateway/.venv/Scripts/python.exe -m pytest tests/unit/test_audit.py tests/unit/test_audit_chain.py tests/unit/test_audit_field_coverage.py -q
```

V2 里 ADR 是 markdown，ruff 只对 .py 生效——`docs/...md` 传入会被忽略（不是失败）。
V1 本机约 2.5–3 分钟，`test_change_pipeline.py` 是大头。

## Forbidden

- 弱化或删除 `tests/unit/test_redact.py` 里任何既有精确断言。
- 为通过而放宽正则到会误杀普通文本的程度（用 criteria 2 的反例检验）。
- 在 `app/redact.py` 里引入随机、环境读取或非确定性行为。
- 把 `ci_triage` 的清单并入本 PR（留 follow-up issue）。
- 改 `.env`、CI 配置、依赖清单、台账、分支保护。
