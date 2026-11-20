# 契约：审计链跨日误报修复（audit WAL date rollover）

- **task id**：`audit-chain-rollover-20261120`
- **分支**：`fix/audit-chain-rollover-20261120`（worktree `.worktrees/audit-chain-rollover`）
- **冻结时间**：2026-11-20
- **所有者签收**：用户消息「现在启动」（授权启动本链路）

## 1. 现象（有证据）

2026-11-20 `/healthz` 返回 `status=degraded`：

```
audit_chain: "degraded: 审计链断裂 audit-2026-11-20.jsonl:1"
```

`logs/audit-2026-11-20.jsonl` 第 1 行与 `logs/audit-2026-11-19.jsonl` 最后一行：

```
last 11-19 writer=LAPTOP-TODOM8HG:20748 entry_hash=0be88ebaf253...
first 11-20 writer=LAPTOP-TODOM8HG:20748 prev_hash=0be88ebaf253...
```

同一写者、`prev_hash` 沿用昨日链尾 → 校验器按「每个日文件一条独立链、首行
`prev_hash` 必须为空」（`app/audit/wal.py:112`，且 `tests/unit/test_audit_chain.py`
`test_chain_links_consecutive_entries` 断言 `rows[0]["prev_hash"] == ""`）判为断裂。

历史同因：`audit-2026-10-21.jsonl:19`（写者 `:3964` 上一笔落在 10-20）。
`audit-2026-09-29.jsonl:25` 是修复前的多写者交错，属遗留，不在本次范围。

## 2. 根因

`AsyncWal.append()`（`app/audit/wal.py:208`）只在 `_last_hash is None` 时恢复链头
（`:211`）。跨午夜后 `_last_hash` 非空，于是新一天首行继续挂在昨天的 `entry_hash`
上；而 ADR-019 与校验器都把「跨日不接续」当作公开契约。

后果不是“链被改过”，而是**每天一次假阳**：只要进程跨夜在线，`/healthz` 整天
`degraded`。这正是 `chain_report` 注释里点名要避免的「永远红和永远绿一样没人看」。

## 3. 允许路径（Allowed paths）

```
app/audit/wal.py
tests/unit/test_audit_chain.py
plans/audit-chain-rollover-20261120.md
```

超出此清单的一律先回主会话重新冻结。

## 4. 验收标准（Acceptance criteria）

- **A1 跨日切换**：同一 `AsyncWal` 实例先写 D 日、再写 D+1 日，D+1 文件首行
  `prev_hash == ""`，且 `verify_chain(D+1 文件) is None`。
- **A2 同日重启不回归**：新实例在当日文件已有本写者行时，仍从本写者链尾续接
  （`test_chain_survives_a_process_restart` 继续绿）。
- **A3 跨日且当日文件已有本写者行**：D+1 文件由他人先写、本写者随后写入时，
  本写者首行应接**当日自己**的链尾；当日无自己行则以空链头起链。
- **A4 篡改检测不变**：改字段 / 改 `prev_hash` / 删行仍被点名到具体行。
- **A5 多写者交错不变**：两个 `_WRITER_ID` 交替写同一文件，`verify_chain is None`；
  删掉其中一行仍报断裂。
- **A6 校验语义不动**：`chain_report` / `verify_chain` / `/healthz` 判定逻辑零改动。
- **A7 历史数据不动**：不改写任何既有 `logs/*.jsonl`；不引入“豁免跨日首行”的特例。

## 5. 验证命令（Verification commands）

```
V1  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit/test_audit_chain.py tests/unit/test_audit.py tests/unit/test_audit_field_coverage.py tests/unit/test_healthz_cache.py -q
V2  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest tests/unit -q
V3  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m pytest -q
V4  $env:PYTHONPATH='.'; .venv/Scripts/python.exe -m ruff check .
```

外加**确定性跨日探针**（不改系统时钟、不 sleep）：用假 `date` 类 patch
`app.audit.wal.date`，同一实例写 D 日与 D+1 日各一条，然后断言
`chain_report(D+1 文件) == (None, 1)` 且首行 `prev_hash == ""`。
Verifier 需亲自重跑该探针，不采信实现方贴的输出。

## 6. 禁止（Forbidden）

- 改 `chain_report` / `verify_chain` 的判定语义，或给跨日首行开豁免口子。
- 改写/迁移既有审计文件以“消红”（历史是事实，红就是红）。
- 修改哈希算法、`_canonical_payload` 覆盖字段、`writer` 标识规则。
- 触碰本契约清单外模块（含 `app/routes/health.py`）。
- 改/删/弱化既有断言换通过；跳测试不记录。

## 7. 已知限制（合并后仍需如实说明）

- 修复只影响**之后**的写入：`audit-2026-11-20.jsonl:1` 已写坏，今天 `/healthz`
  在跨过本日文件前仍会 `degraded`（次日新文件起恢复正常）。
- 修复需重启 8083 才生效。
- 09-29 的历史断裂不可修，继续如实保留。
