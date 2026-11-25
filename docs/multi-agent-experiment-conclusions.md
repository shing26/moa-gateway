# 多智能体剂量实验：结论归档

> 状态：**已冻结（2026-10-25 定稿，2026-11-25 归档）**。本文只保存历史实验的结论与复跑口径，
> 不代表项目当前主线。项目主线是受治理的仓库交付 Agent；协作图与 `bench/` 剂量实验均不在
> 产品主路径上。
>
> 机制说明见 [multi-agent-experiment-bench.md](multi-agent-experiment-bench.md)。

## 可验证实验台（Experiment Bench）

剂量曲线之外，`bench/experiment/` 把每次执行统一落成
`Experiment → Trial → Run Record`：任务、模型与配置快照、真实 prompt 指纹、计划与依赖、
Critic/Guard/HITL 结果、token/成本/工具账本、工作区执行前后指纹都在同一条记录里。
每条 Trial 独立写本地不可变 JSON，重复 Experiment ID 或覆盖旧 Trial 会直接失败。

```powershell
$verify = @("python", "-m", "pytest", "-q") | ConvertTo-Json -Compress
.\.venv\Scripts\python.exe bench/run_experiment.py collab `
  --task "读取 README.md 并总结" --mode mock --workspace . `
  --verify-command-json $verify --out .bench-experiments/readme-check
```

本地真模型用 `--mode live --provider local --model qwen2.5:3b`；受控复跑传
`--compare-to <baseline>/experiment.json`。

"可验证"指机制（不可变 Run Record、预注册分析、全量对账）可被任何人复跑检查，
不等于结论已在多配置下验证：正式批次结论目前只覆盖两个本地模型配置
（v5/v8 均为 qwen2.5:3b，差异在 temperature 与单次输出上限），NVIDIA 端点
批次因容量阻塞未形成可分析样本，跨模型报告固定为描述性并列、不作推断。

## 第一轮真实曲线（2026-10-19）

模型 `nvidia/nemotron-3-super-120b-a12b`（免费端点，`temperature=1.0`），2 题 × 4 档 × 1 次：

| 档 | 通过率 | 平均 token | 平均每 pass token |
| :---: | ---: | ---: | ---: |
| L0 | 50% | 165,071 | 330,141 |
| **L1** | **100%** | 248,192 | 248,192 |
| L2 | 50%（1 条撞预算） | 371,190 | 742,380 |
| L3 | 50% | 217,867 | 435,733 |

边际：**L0→L1 +50pp / +83k token；L1→L2 −50pp / +123k；L2→L3 +0pp / −153k。**
读法是"**L1 是峰值**"：同样的模型、同样的题，只加一条 Critic 回边就把其中一题从 fail
翻成 pass；再往上加 planner（L2）成本涨了、通过率反而掉回来。L3 的 token 比 L2 低，
是因为它把活分给并发 worker，但通过率没比 L2 更好。

> [!IMPORTANT]
> **这是方向性证据，不是统计结论**：`repeats=1`（报告里每个格子都标 `under_sampled`）、
> 只有 2 题。单个 pass/fail 翻转就足以改变任何一列。

复现：`uv run python bench/run_curve.py --tasks 988fccb a746b81 --repeats 3`
（**真花钱、不进 CI**），归档在 `.bench-work/`（不入库）。

## 正式批次结论（结论日期 2026-10-24）

**口径**：v5 B1（采集 2026-10-23）授权分析 + v5/v8 B1/B3 四批次
（2026-10-23 至 2026-10-24）384 条 Run Record 全量对账。
只有 v5 B1 满足 `analysis_authorized=true`；v8 B1 被 L3 有效样本门禁挡住，
v8 B3 存在 17 个截断 `error`。四批次全部对账通过，但对账 ≠ 授权分析。

| 批次 | 结论 | L0 token | L2 token | L3 token |
| :--- | :--- | ---: | ---: | ---: |
| v5 B1（授权分析） | 四 arm 通过率均 0%，L3−L0 配对差 0.0，p=1.0 | 67,884 | 705,993 | 538,632 |
| v8 B1 | 不可授权分析；L0 通过率 0% | 95,582 | 1,410,024 | 705,529 |

**读法必须精确**：L0 通过率本身为 0%，所以"配对差 0.0"不能解读为
"多智能体无增益"，只能解读为"当前模型与任务集下基线贴地板，增益不可测量"。
不受地板效应污染的硬数字是 token 倍率：L2/L3 相对 L0 消耗 **7.9–14.8 倍**。
**0 可测增益 × 8–15 倍成本**，是协作默认关闭的直接依据；默认开启的前提是
基线通过率脱离地板后，新批次给出配对检验显著为正。

这条教训已固化为 pilot 区分度门禁（`scripts/verify_pilot_runtime.py`）：
正式批次授权前，基线 arm 必须有足够有效样本且通过率不得贴 0%/100%；
落在 20%–80% 建议区间外会产生功效警告。v8 当年的 pilot 只有 1 条 L0
样本且全 fail，若该门禁当时存在，正式批次会在授权阶段被挡下，而不是
跑满 96 条后才发现数据没有区分力。

## 第二任务域：Issue 分诊（2026-10-25 收口）

分诊轨道复用同一条 pilot 门禁与 Run Record 契约，任务判定为 category+route
全对（priority 仅记录）。三轮 pilot 各拦截一个根因（workspace 空转 → 判定口径
含主观字段 → L2 planner 结构性失败），第四轮按 runbook 处置方向 2 冻结
**v3 L0-only 协议**（`scope.multi_arm_comparison=false`，无比较对象时
`analysis_authorized=false` 是正确输出）：

- pilot `issue-triage-v3-l0-pilot-live-r2`：基线 37.5%，门禁 `ready=true`；
- formal `issue-triage-v3-l0-formal-b1-live-r1`：24/24、0 error、全量对账通过，
  **L0 通过率 25%**，平均 1,938.7 token/次；逐字段归因 category 83.3% / route 37.5%，
  失败集中在 route 误路由 human。

读法：这是**方法迁移与基线建立**，不是分诊能力宣称——25% 只属于
qwen2.5:3b + 512 这一配置。多档重进须新协议重过门禁；主线编码任务的多智能体
结论以 v5 B1 授权分析为终稿，不再用当前配置追加批次。
