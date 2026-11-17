# ADR-039: 异常处理债两笔——BLE001 三步开启与 Result 接口预注册

日期：2026-11-16
状态：第一步（BLE001 盘点与收敛）已实施，规则的最终开启被范围冲突阻塞（2026-11-17，见文末「第一步实施记录」）
关联：ADR-037（输出守卫 fail-closed，已实施）、`app/orchestration/collaboration.py`
的 `expert_failure_reason` 嗅探层、外部锐评（2026-11-16）第二/三条指控

## 背景

锐评指出"fail-closed 只活在中间件和文档里，没有活在类型和 lint 里"。查证属实，
其 lint 半边已由 PR #97 部分修复（`E722`/`F821` 开启，3 处未定义名补 import）。
剩余两笔债，成因同源，故同文记录：

1. **`BLE001` 仍在 ignore 列表**：`app/` 内 112 处 `except Exception`。
   其中一部分是**健康的**（重试边界的兜底、审计"不拖垮主流程"的既定设计，
   如 `app/audit/recorder.py`），一部分正是 fail-open 的温床——ADR-037 修的
   三站点就是它们。规则不开，同类问题还会再长出来。
2. **失败与成功共用一条通道**：`SubAgent.execute() -> str`，失败靠
   `expert_failure_reason` 在字符串上嗅探（异常前缀正则、凭据名、自家失败
   文案）。嗅探层是坏接口上的创可贴：误判面永久存在，类型系统帮不上忙。

## 决策

**1. BLE001 三步开启，作为独立任务立项（不并入其他修复）：**

1. **盘点**：112 处逐一定性为三类——健康兜底（加 `# noqa: BLE001` + 一句
   理由）、该 fail-closed 的（改语义或收窄）、可收窄异常类型的（改 except 子句）；
2. **收敛**：按类处理，每类一个 commit，全程 `tests/unit` 绿；
3. **开启**：从 ignore 移除 `BLE001`，`ruff check .` 全绿，与 E722/F821 同批
   的"规则真实生效"判据（`--select BLE001` 零命中）照抄。

触发点：窗口后任意时间，预估一次独立验收链路会话（worktree + verifier）。
**禁止**在功能性修复的 PR 里顺手挪——112 处的 diff 会淹没功能变更。

**2. `Result[Output, Failure]` 接口预注册：**

- 目标形态：专家执行返回结构化结果（成功 payload / 失败原因 + 类别），
  `expert_failure_reason` 嗅探层整体退役；
- 实施时机（满足其一）：下次修改 `collaboration.py` 执行接口时**一并**做
  （避免为它单独发起大重构）；或嗅探层再次实证误判（漏判或误升级）时立项；
- 实施约束：必须走验收链路；嗅探层退役前保持双轨，不删测试。

## 后果

- 正面：两笔债有编号、有步骤、有触发点——它们从"锐评里的情绪"变成待办；
  BLE001 开启后，"新 except Exception 必须声明理由"成为 lint 强制。
- 负面（如实）：嗅探层在 Result 落地前继续存在，误判面继续存在——已知的、
  有出口的债，不是被遗忘的债。
- 边界：预注册部分不改变运行时行为；第一步实施记录中的 B 类改动已改变一处运行时语义
  （隐私删除 fail-closed），见该记录。

## 第一步实施记录（2026-11-17）

**基线。** `app/` 内 `except Exception` 共 **112** 处。注意 ruff 0.16.3 的 BLE001 实测行为：
处理器里用 `logger.exception` / `exc_info=True` 记录异常、或处理器重新抛出异常时，规则不报。
因此 `ruff check --select BLE001 app` 在 base 上只报 **54** 处（28 处已有 `# noqa`，其中
20 处被 noqa 抑制）；加 `--ignore-noqa` 报 **74** 处；与 112 的差额 38 处正是上述豁免。
契约「分类总数 = base 命中数」的前提与 ruff 实际行为不符，本步以 **112**（全部真实站点）
为盘点分母。

**逐处定性（合计 112）。**

- **A 健康兜底：107 处。** 全部保留 `except Exception`，加 `# noqa: BLE001 - <一句理由>`。
  106 处已落地；`app/routes/dashboard.py:247` 因契约 Forbidden 禁止触碰而**未加**（见「阻塞」）。
- **B 该 fail-closed：1 处**——`app/routes/health.py:142`（`privacy_erase`）。
  旧行为：向量删除失败仍返回 `200 {"status": "ok"}`，错误只塞进 `deleted.vectordb`；
  新行为：记录后原样上抛，由统一错误契约返回 500；更安全：隐私删除失败不再伪装成成功。
  覆盖测试：`tests/unit/test_privacy_erase.py::test_privacy_erase_fails_closed_when_vectordb_delete_fails`。
- **C 可收窄：4 处**——`app/channels/feishu_event.py:36`（`json.loads` →
  `JSONDecodeError`/`RecursionError`）、`app/engine.py:268` 与 `:283`（`json.loads` +
  pydantic → `TypeError`/`ValueError`/`RecursionError`）、`app/memory.py:249`（同上）。
  四处都是 JSON 解析边界：`ValueError` 已覆盖 `JSONDecodeError`/`UnicodeDecodeError` 与
  pydantic `ValidationError`，收窄后不会漏。

**阻塞（本步的「开启」未完成）。**

1. `pyproject.toml` 的 `ignore` 仍含 `BLE001`。删除它会在 `ruff check .` 下暴露 **42 处**
   `app/` 之外的命中（`apps/code_review_pipeline/`、`bench/`、`eval/`、`evals/`），全部在
   本契约 Allowed paths 之外；CI 的 Lint 步骤正是 `ruff check .`。
2. `app/routes/dashboard.py:247` 是 112 处之一且会被 BLE001 报出，但该文件在 Forbidden 列表。

两项都需要扩大契约范围（把 `apps/`、`bench/`、`eval/`、`evals/` 与 `dashboard.py` 纳入
Allowed paths）后才可执行「开启」。在此之前保留 `ignore`，`ruff check .` 全绿；
`ruff check --select BLE001 app` 仍有且仅有 `dashboard.py:247` 一处。按契约 Forbidden，
未越界修改。

**未做：§决策 2（`Result[Output, Failure]` 接口）。** 触发条件不变：下次修改
`app/orchestration/collaboration.py` 执行接口时一并做，或嗅探层再次实证误判时立项。
