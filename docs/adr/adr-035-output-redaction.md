# ADR-035: 输出侧统一脱敏，只接两处汇聚点

日期：2026-11-07
状态：已实施
关联：ADR-034（agent 执行身份 = 所有者 PAT）、`app/agents/subprocess_env.py`
（输入侧白名单）、`app/redact.py`、`app/audit/models.py`、
`apps/code_review_pipeline/change/pipeline.py`

## 背景

凭证防线的主力是**输入侧**的 `subprocess_env`：白名单环境变量，保证子进程
"拿不到"新凭证。输出侧并非完全空白——`apps/code_review_pipeline/delivery/ci_triage.py`
自 S0 起就有 `redact_ci_log()`，对 CI 日志引文做局部脱敏（ANSI 清洗 + 固定
`[REDACTED]` 占位）；但它只覆盖那一条证据链，其余输出没有统一层。治理运行时
会把三类**不可信内容**写进持久化位置——

1. agent 输出（实现 Implementer 的返回，进审计 WAL / ES）；
2. verify 命令回显（进 bundle 的 `verify_output`）；
3. CI 日志引文（`ci_triage.py` 的报告正文，引用的是自己都声明 untrusted 的
   日志数据）——仍由其自带的 `redact_ci_log()` 处理，未并入本模块（见「后果」
   的已知边界）。

这些内容一旦带着密钥落盘或外发，就是删不干净的长期留痕：GitHub 上的 PR
正文 / commit message 进 git 历史，WAL 的哈希链让"事后悄悄删掉那一行"本身
成为一次被记录的断裂。而 ADR-034 的决策（agent 以所有者 fine-grained PAT
执行）使 `github_pat_*` 成为真实存在于运行环境的密钥形态——这条路径不是
理论风险。

外部佐证：独立项目（dsh-session-superagent，2026-11 观察）在同一问题上给出
同构解——"凭证不进模型"之外再配一层统一 `redact()`。两个独立实现趋同，
说明这不是过度设计。

## 决策

1. **新增 `app/redact.py`**：`redact(text)` 与 `redact_value(value)`（递归
   容器）。八类高置信度形态：GitHub token（经典 + fine-grained）、OpenAI
   `sk-`、AWS `AKIA`、Bearer、Authorization 头（保留 Basic / token / Digest /
   Bearer 等 scheme，只换凭证）、PEM 私钥、URL 内嵌凭证、通用 `key=value`
   （含 `DB_PASSWORD` 这类带前缀变量名与 `{"password": "..."}` 这类 JSON 字段
   形态）。占位符统一 `[REDACTED:<kind>]`——grep 可盘点，不静默。
2. **只接两处汇聚点，不在调用方散接**：
   - `AuditEntry.to_audit_dict()`——WAL 与 ES 的唯一序列化出口。哈希链覆盖
     的就是脱敏后落盘的内容；`agent_output_len` 取脱敏后长度，与落盘一致。
   - `change/pipeline.py` 的建 commit / 开 PR——外发 GitHub 的唯一写口。
3. **确定性约束**：同一输入永远同一输出。WAL 哈希链要跨机器复算，禁止任何
   随机或依赖环境的替换。
4. **保守原则**：只匹配高置信度形态（长度下限取"该形态最短合法值"），
   宁漏勿误。审计可读性本身是交付物——误杀把审计变得不可读，等于变相
   关掉审计（同 2026-09-28 "永远红"教训的逻辑）。

**被拒备选**：在 `guard` 层加一道"输出检查"拦截——拒绝：guard 拦的是
**放行决策**，脱敏是**内容变换**，语义不同；且多一层拦截就多一处漏接点。

## 后果

- 正面：凭证防线从以输入侧为主变成纵深（输入 + 输出落盘 + 输出外发），
  三处都有可点开的 artifact；`app/redact.py` **内部**的形态清单集中在
  `_PATTERNS`，测试逐形态覆盖，且含确定性判据与"普通审计文本不误杀"的反例。
- 已知边界（如实陈述）：仓库里目前是**两套**脱敏正则——本模块的 `_PATTERNS`
  （占位符 `[REDACTED:<kind>]`）与 `apps/code_review_pipeline/delivery/ci_triage.py`
  的 `_REDACTIONS`（占位符 `[REDACTED]`，另含 ANSI 清洗）。两者形态清单与
  占位符均未统一，本 ADR 记录时也未合并；后续方向是单独 PR 把 CI triage
  的清单收敛到同一套正则/占位符，并为 CI 日志补上与 `_PATTERNS` 同级的
  回归用例。在此之前，"某形态被脱敏"只对本模块覆盖的出口成立。
- 负面（如实陈述）：误杀风险仍存在——普通文本若恰好长得像密钥（如 ≥12 字符
  的 `password: ...` 值）会被占位符替换；接受，因为漏杀的代价（密钥永久
  留痕）远高于误杀的代价（审计里多一个占位符）。
- 边界（如实陈述）：审计看到的是**脱敏后**的内容——若确需原文排障，WAL
  不再是来源，本地 trace 目录里的原始产物才是。哈希链证明"这段脱敏内容
  自写入起未被改动"，不证明"原文如此"。
- 形态清单是**移动目标**：新的密钥形态（新厂商、新前缀）需要追加模式与
  测试样例；追加时保持"宁漏勿误"的门槛原则不变。
