# ADR-039: 异常处理债两笔——BLE001 三步开启与 Result 接口预注册

日期：2026-11-16
状态：第一步（BLE001 盘点与收敛）已实施，规则已开启（2026-11-17，见文末「第一步实施记录」）
关联：ADR-037（输出守卫 fail-closed，已实施）、`app/orchestration/collaboration.py`
的 `expert_failure_reason` 嗅探层、外部锐评（2026-11-16）第二/三条指控

## 背景

锐评指出"fail-closed 只活在中间件和文档里，没有活在类型和 lint 里"。查证属实，
其 lint 半边已由 PR #97 部分修复（`E722`/`F821` 开启，3 处未定义名补 import）。
剩余两笔债，成因同源，故同文记录：

1. **`BLE001` 仍在 ignore 列表**：盘点范围内共 190 处 `except Exception`
   （`app/` 112、`apps/` 65、`eval/` 6、`evals/` 2、`bench/` 5）。
   其中一部分是**健康的**（重试边界的兜底、审计"不拖垮主流程"的既定设计，
   如 `app/audit/recorder.py`），一部分正是 fail-open 的温床——ADR-037 修的
   三站点就是它们。规则不开，同类问题还会再长出来。
2. **失败与成功共用一条通道**：`SubAgent.execute() -> str`，失败靠
   `expert_failure_reason` 在字符串上嗅探（异常前缀正则、凭据名、自家失败
   文案）。嗅探层是坏接口上的创可贴：误判面永久存在，类型系统帮不上忙。

## 决策

**1. BLE001 三步开启，作为独立任务立项（不并入其他修复）：**

1. **盘点**：190 处逐一定性为三类——健康兜底（加 `# noqa: BLE001` + 一句
   理由）、该 fail-closed 的（改语义或收窄）、可收窄异常类型的（改 except 子句）；
2. **收敛**：按类处理，每类一个 commit，全程 `tests/unit` 绿；
3. **开启**：从 ignore 移除 `BLE001`，`ruff check .` 全绿，与 E722/F821 同批
   的"规则真实生效"判据（`--select BLE001` 零命中）照抄。

触发点：窗口后任意时间，预估一次独立验收链路会话（worktree + verifier）。
**禁止**在功能性修复的 PR 里顺手挪——近 200 处的 diff 会淹没功能变更。

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

**基线。** 真实站点共 **190** 处（`app/` 112 + `apps/` 65 + `eval/` 6 + `evals/` 2 +
`bench/` 5）。注意 ruff 0.16.3 的 BLE001 实测行为：处理器里用 `logger.exception` /
`exc_info=True` 记录异常、或处理器重新抛出异常时，规则不报；存量 `# noqa` 也会抑制命中。
因此 `ruff check --select BLE001 .` 的命中数低于真实站点数，本步以 **190**（全部真实站点）
为盘点分母，而不是以 lint 命中数为分母。

**逐处定性（合计 190）。**

- **A 健康兜底：182 处**（`app/` 107、`apps/` 62、`eval/` 6、`evals/` 2、`bench/` 5）。
  全部保留 `except Exception`，加 `# noqa: BLE001 - <一句理由>`；理由写入代码，
  verifier 可按「为什么兜住是安全的」逐条复核。
- **B 该 fail-closed：1 处**——`app/routes/health.py:142`（`privacy_erase`）。
  旧行为：向量删除失败仍返回 `200 {"status": "ok"}`，错误只塞进 `deleted.vectordb`；
  新行为：记录后原样上抛，由统一错误契约返回 500；更安全：隐私删除失败不再伪装成成功。
  覆盖测试：`tests/unit/test_privacy_erase.py::test_privacy_erase_fails_closed_when_vectordb_delete_fails`。
- **C 可收窄：7 处。**
  - app 4 处：`app/channels/feishu_event.py:36`、`app/engine.py:268` 与 `:283`、
    `app/memory.py:249`，都是 JSON 解析边界（`ValueError` 已覆盖
    `JSONDecodeError`/`UnicodeDecodeError` 与 pydantic `ValidationError`）。
  - 非 app 3 处：`apps/code_review_pipeline/agents/code_review_pipeline.py:635`
    与 `agents/report_agent.py:108`（`json.loads(str)` →
    `JSONDecodeError`/`RecursionError`）、`routing/github_client.py:213`
    （`resp.json()` + `.get` → `AttributeError`/`ValueError`/`RecursionError`）。
  每处收窄后仍保留原有的失败降级行为，没有把异常漏给调用方。

**开启已落地。** 从 `pyproject.toml` 的 `ignore` 移除 `BLE001`；未使用 per-file-ignores /
extend-ignore。`ruff check --select BLE001 .` 零命中，`ruff check .` 全绿，`tests/unit` 全绿。

**交付链路复核。** `apps/` 树 65 处逐处确认，重点核对 `delivery/` 链路本体：失败都被终结为
`NEEDS_HUMAN`/`FAILED`、转换成 `PushRecoveryRequired`，或只影响派生视图/观测信号；
没有「失败被吞掉后推送/合并继续」的路径，因此该树 B 类为 0。`storage/review_store.py:965`
的 `get()` 只读失败返回 `None`，没有生产调用方据此做写决策（仅契约/测试使用），按 A 处理。

**未做：§决策 2（`Result[Output, Failure]` 接口）。** 触发条件不变：下次修改
`app/orchestration/collaboration.py` 执行接口时一并做，或嗅探层再次实证误判时立项。
