# ADR-034: Agent 执行身份用所有者 PAT，人机归因靠 trace trailer

日期：2026-11-01
状态：已实施
关联：ADR-033（dogfood 台账）、`docs/acceptance/dogfood-ledger.md`、
commit trailer `MoA-Trace-Id`

## 背景

dogfood 交付（ADR-033）需要回答"这行交付是谁做的"。三个候选：

- **A. 所有者个人 fine-grained PAT**：按仓库授权（contents/pull-requests 写、
  actions 读），配置最轻。
- **B. 独立 agent 账号**：人机在 GitHub 侧天然分离，但多一个账号要维护，
  且单人场景下"批准人与执行人分离"流于形式。
- **C. GitHub App**：权限粒度最正规，但安装、webhook、JWT 维护对单人是超重配置。

核心矛盾：账号只承载**推送身份**，不承载**归因语义**。真正的归因证据是提交信息里的
`MoA-Trace-Id: delivery-…` trailer（ADR-033 交付链路已强制），它不依赖账号。

## 决策

1. **起步用 A**：agent 以所有者 fine-grained PAT 执行，权限按仓库最小授予。
2. **人机归因依据 trailer，不依据账号。** git 历史显示 agent 提交来自所有者账号
   是**已知并接受**的后果，不视为归因缺陷。
3. **升级 B 的客观触发点**（满足其一即升级，不凭感觉）：
   - 台账开始被第三方引用为证据（简历、面试、外部分析）；
   - 出现多 agent 并发交付，trace trailer 不足以区分执行实例。
4. 触发点满足前，不预建账号、不预配 App。

## 后果

- 正面：交付零配置起步；归因审计已有 trace trailer 承担；升级路径有明确判据，
  避免"永远差一步就换正规方案"的拖延。
- 负面：平台侧（GitHub UI）无法直观区分人机操作；接受。
- 台账每行仍须记录执行身份来源（"所有者 PAT"），第三方引用时如实陈述。
