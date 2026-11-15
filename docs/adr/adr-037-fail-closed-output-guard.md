# ADR-037: 输出守卫异常路径 fail-closed，升级 REVIEW 走 HITL

日期：2026-11-16
状态：已实施
关联：`app/guard/guard_service.py`（三态语义与未配置默认值）、
`app/pipeline.py`、`app/orchestration/graph.py`、`app/orchestration/collaboration.py`

## 背景

输出守卫 `guard_service.evaluate_output` 的调用方在三个站点（请求路径与两个
编排运行时）都用宽捕获兜底守卫自身异常：

- `app/pipeline.py`（请求路径）
- `app/orchestration/graph.py`（LangGraph 运行时）
- `app/orchestration/collaboration.py`（协作运行时）

三处旧实现均为 `except Exception: output_verdict = GuardVerdict(action=ALLOW,
reason="ok")`——守卫自己崩溃时**把失败当放行**。治理系统最该 fail-closed 的
位置恰好是 fail-open：守卫失明期间，本应被拦的危险输出照样以 `status="ok"`
送达，且该决策从未有过 ADR 留痕。

## 决策

1. **异常时 verdict 升级为 `GuardianAction.REVIEW`**（三态语义见
   `app/guard/guard_service.py:29`，REVIEW 走 HITL 审批卡），reason 必须携带
   异常类型与简要消息（`output guard failed closed: ValueError: ...`），
   不许吞掉异常细节——审批人需要知道守卫为什么失明。
2. **保持宽捕获、不向上抛**：守卫自身崩溃不应当炸掉请求主流程（可故障转移），
   但必须升级人工。这就是 fail-closed 在本系统的具体含义：不是"抛异常"，
   而是"不放行"。
3. **正常路径零变化**：`evaluate_output` 正常返回时，判定、合并
   （`_merge_guard` 优先级 DENY > REVIEW）、后续流程一律不动。
4. 三个站点保持同一份 except 块语义（逐行镜像），由双运行时的 golden parity
   门禁（`tests/unit/test_engine_parity_golden.py`）守护一致性。

### 被拒备选

- **收窄异常类型**（只捕特定异常）：守卫依赖的策略引擎、存储等边界随时可能
  抛出新类型，收窄会重新打开 fail-open 的口子。
- **向上抛出**：守卫崩溃直接变成请求 500。守卫是治理组件，不应比业务主流程
  更脆；且 500 没有人工升级闭环，REVIEW 有（store_hitl → 审批卡 → 回调）。

## 边界

`guard_service.py:126` 的"未配置守卫时默认 ALLOW"是另一项文档化的决策
（无守卫 ≠ 守卫崩溃），不在本次修复范围内。

## 后果

- 正面：守卫失明时输出不再静默放行，而是进入有人看、有留痕、可回流的 HITL
  通道；异常细节进审批单 reason，事后可审计守卫故障。
- 负面：守卫故障期间的请求不再自动完成，需人工裁决——接受，这正是
  fail-closed 的成本与意义。
- 配套：ruff ignore 列表移除 `E722`、`F821`（裸 except 与未定义名从此可检出），
  因开启 F821 需补的三处 import 已一并修复。
