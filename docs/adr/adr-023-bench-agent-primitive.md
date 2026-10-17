# ADR-023: bench 内的 agent 原语，自报与判决分离

日期：2026-10-17
状态：已实施
关联：ADR-018（工具调用契约）、`docs/superpowers/specs/2026-10-15-multiagent-collaboration-benchmark-design.md`（§5.0）

## 背景

项目要跑"多智能体协作的边际收益"实验，缺的第一个东西不是协作，而是**一个真 agent**
（工具 + 循环 + 目标）。而仓库里现有的东西都不是：`SubAgent` 的协议是
`execute() -> str`，`TaskResult` 没有任何"结局"字段，`code_review_pipeline` 的五个
审查阶段是五个顺序 `await`。类型上它们**永远在成功**——而一个不会失败的执行体，
剂量曲线测不出任何东西，因为所有档位都会"通过"。

## 决策

1. **原语落在 `bench/agent/`，`app/` 零改动。** 接口按"将来要转正"的形状设计，
   但这次不动 `app/agent_core`：那里有 `SubAgent` 协议与 1180 条测试钉着的表面，
   而实验需要的是**先能跑通**，不是先统一实现。
2. **自报与判决分离成两个类型。** `AgentRun` 是执行体对自己为什么停下的陈述，
   `RunResult` 是裁判的认定。两个类型各有自己的结局字面量：`fail` 只在
   `Outcome` 里（"没通过验证"是裁判的事，执行体无权说）；`budget_exhausted` /
   `error` 产生于执行体、由 harness 如实转记——它们的意思是"**没有判决可下**"，
   而不是"判决为不达标"。§7 的全部防作弊设计都建立在"agent 不能自己下判决"上，
   把两者挤进一个类型，审计时说不清 `passed: true` 是谁说的。
3. **在线动作协议唯一为原生 tool-calling**（复用 `chat_with_tools`）。仓库里现有
   `ReActLoop` 的 JSON-in-text 与 `stubs._execute_with_tools` 的 tool_calls 两套
   协议并存，正是 ADR-018 当初要消灭的"同一概念两条路径"。离线确定性改由
   **与在线同接口的 scripted 决策器**提供，而不是再引入第二套协议。
4. **`run_tests` 的基底命令由任务定义固定，模型只能追加筛选参数**（白名单制）。
   行动空间因此有界，L0–L3 的对比少一个自由度。
5. **美元预算事后累计，允许最后一次调用越额。** `cost_usd` 只能事后从
   `last_metrics` 读到，事前无从预测；判据是"累计值已达到上限"，越额有界且如实
   记录。判停顺序是**先判完成、再判预算**——模型刚好在越额那一轮交了终局，那它确实
   完成了，记成预算耗尽是把成功说成失败。

## 验收

1. 循环在四个自报结局上都可观测：`completed` / `step_limit` / `budget_exhausted`
   / `error`；同一轮"既完成又越额"记 `completed`（不是预算耗尽）。
2. 工具被拒、参数非法、工具不存在三类都不中断循环，各自以 observation 回流，且
   `rejected` 与 `failed` 分开记账（沿用 ADR-018 决策 3）。
3. 路径逃逸（含符号链接）判 `rejected`；`run_tests` 只能追加白名单筛选，基底命令
   不可被模型替换。
4. 物化后的工作区只有一个提交、无原始历史；fail-first 必须失败，否则任务判无效
   并跳过。
5. 验证之前，整棵测试树回到 test patch 版本（含不在 `test_paths` 里的收集钩子），
   树里新增的未跟踪文件被清除。
6. `RunResult` 如实记录实际作答的模型（fallback 链接手时不写主模型名）与温度。
7. `pytest tests/unit -q` 零回归；`bench/run_task.py` 的人工验收能产出 `pass`
   或诚实的 `fail`，并落一份完整 `RunResult` JSON。

## 后果

- **短期内有两套工具循环**：`app/agent_core/react.py` 的 `ReActLoop` 与
  `bench/agent/loop.py`。这是明知故犯的重复，转正时应当合并或下线其一；在那之前
  这条债记在这里，而不是靠"反正以后会统一"含糊过去。
- **`Outcome` 把"没有判决可下"与"判决结果"放在同一个字面量里**（`pass` / `fail`
   / `budget_exhausted` / `error`）。刻意如此：聚合剂量曲线时四种格子都要能直接
   落桶，拆成"判决 + 停止原因"两个字段会让每处聚合点都读两处。代价是读 `outcome`
   的人必须知道后两者不是"没达标"。L0–L3 沿用，只允许新增字段。
- **`AgentRun.model` 记的是实际作答的模型**（取自 `last_metrics["model_used"]`），
   所以同一个 arm 的不同 run 可能落不同的模型名——那是如实，不是不稳定。
- **实验台依赖完整 git 历史**（物化要用 `git archive <基线提交>`）。浅克隆下任务
  无法物化，对应用例会 skip，而不是伪装通过。
- **网络只是软约束**：harness 剥掉代理与凭据类环境变量，工具集里没有联网能力，
  但不进容器。真隔离是另一个量级的工程。
