# 09 — 真实 API 跑一轮四档曲线（人工验收）

**What to build:** 前面全是离线确定性，这一条才是实验本身：用真实中档模型在任务集上把 L0–L3 各跑一遍，产出曲线与一份完整报告——卡住也照实记。花钱、不进 CI。收工时要么给出一条曲线，要么给出"在当前预算下测不出差异"这个同样值钱的结论。

**Blocked by:** 01 — 任务集；06 — L3；07 — 预算策略；08 — 报告

**Status:** ready — key/端点/工具链全通；模型是**免费**的（`cost_usd` 恒为 0），已按
§5.3 降级路径落成 **token 主轴**（`BudgetPolicy.axis="tokens"` + 报告 token 列），
真实 token smoke 复验通过。差最后一步：跑一整轮曲线（数小时）。

- [x] 选定中档模型：`nvidia/nemotron-3.5-super-vl-120b-a12b`（NVIDIA integrate API，
      `https://integrate.api.nvidia.com/v1`）。已写入 `.env`（`LLM_PROVIDER=nvidia`）；
      实际 `model` 值由 `RunResult.agent_run.model` 如实落盘。是否支持 native
      tool-calling 由首跑验证——不支持会落 `outcome=error`，这本身也是结论。
- [ ] L0–L3 在同一任务集、同一 token 预算下各跑完
- [ ] 失败的档位按 B3 放宽后再跑一遍，标注增量成本
- [ ] 产出曲线 + 完整 `RunResult` 归档
- [x] 报告里每个数字都能追到具体的 run（08 的报告带 `sources` 来源文件名清单）
- [x] 不把结果并进 CI（`bench/run_task.py` / `bench/report_curve.py` 都不在 `tests/`）

**解除过程：** 最初的阻塞是这台机器的 LLM 配置指向**本机小模型**
（`LLM_PROVIDER=local` / `LLM_MODEL=moa-qwen`），而 §13/§12 明确"本机小模型不参与
bench""模型走 API 中档"——照跑只会得到"所有档位都失败"的假结论，正是 §8 要避免的
噪音。现在 `.env` 的 `LLM_*` 已改指 NVIDIA integrate API、key 已填，本地 Ollama 配置
留在同文件末尾注释里备切换。**当前不再被外部阻塞**；剩下的是自己跑完整轮（数小时）。

**端点踩坑记录（配好即验过）：** LiteLLM 不认 `nvidia/` 前缀，直接传
`model=nvidia/nemotron-3.5-super-vl-120b-a12b` 会抛 `BadRequestError: LLM Provider
NOT provided`。仓库 `app/agents/provider.py::_custom_provider_for` 在 `base_url` 含
`nvidia` 时强制 `custom_llm_provider="openai"`，于是模型名原样透传、`api_base` 指向
integrate 端点。实测 `LLMConfig.from_env("LLM")` + `_build_kwargs` 产出
`{model: nvidia/nemotron-3.5-super-vl-120b-a12b, custom_llm_provider: openai,
api_base: https://integrate.api.nvidia.com/v1}`，正确。

**首次真实模型 smoke（988fccb / L0 / `--max-steps 8`，非曲线数据）：** 全链路打通——
物化 → fail-first（退出码 1）→ 工具循环（8 次 LLM 调用）→ 采集 patch → 还原 → 验证。
落盘 `outcome=fail status=step_limit steps=14 cost=$0.000000 wall=107.4s
tests_touched=False`。观察到三件事：

1. **原生 tool-calling 可用**：模型正确返回 `tool_calls`（探针里 `get_time`、bench 里
   `list_files`/`read_file`/`run_tests` 都真跑了）。这是硬前提，过了。
2. **它是 reasoning 模型**：`reasoning_content` 独立字段、`reasoning_tokens` 计入
   completion。`max_tokens=16` 时推理吃光配额、`content` 返回空——bench 不能给太小的
   token 上限（`LLMConfig.max_tokens` 默认 4096，够用）。
3. **美元轴是死的**：`litellm.completion_cost` 直接抛 `This model isn't mapped yet`，
   provider 也不返回成本（`response_cost=None`、`_hidden_params` 无成本）。后果是 B1
   「等预算」按美元根本不触发、`budget_exhausted` 不会因钱触发、报告成本列全是 $0。
   §5.3 的主判据因此不成立。

   **已解决（免费模型 → §5.3 降级路径）**：`BudgetPolicy.axis="tokens"` 把主轴切到
   token、美元这条置 `inf`；报告加了 token 列；B3 的增量也按 token 记。复验：
   `--budget-axis tokens --main-tokens 20000` 在 25,467 token 处停下，落
   `outcome=budget_exhausted status=budget_exhausted wall=74.6s`——判停确实由 token
   触发，美元上限没干扰。

**轨迹里的能力信号**：8 步全花在侦察（列文件 → 读 ci.yml/CONTEXT.md → 列 tests →
跑两次全量单测 → 读 test_auth.py/test_rag.py），从没走到写修复就到步数上限。另，
模型反复用 `read_file(path, offset, limit)`，而 bench 的 `read_file` 未声明
`offset`/`limit` → 16 次工具调用里 6 次 `rejected`。这是模型没守 schema（本身就是
能力信号），也可能提示工具集该支持分页读。

**成本/时间体量提醒**：此模型 ~13s/次调用、8 次侦察烧了 57,712 prompt tokens。全套
5 题 × 4 档 × 3 次 = 60 run 若都跑满 30 步，是数小时量级。跑曲线前建议先把第一条
的决定（美元 or token 轴）与步数上限一起定下来再压题量。

**顺带修掉的真实缺陷（已改，随本票提交）：** `bench/run_task.py` 从不加载 `.env`，
所以 `LLMConfig.from_env("LLM")` 退回默认值 `provider=direct` / `gpt-4o-mini` /
`api.openai.com`——第一次人工验收就是这么挂的（`outcome=error`、
`litellm.InternalServerError - Connection error`）。现在按项目既有约定
`import app.config`（副作用 `load_dotenv()`，与 `scripts/compare_rerank.py` 同一条），
并加一条守卫用例钉住它。修好后同一条命令用上了配置里的模型（`openai/moa-qwen`）。

**已跑通的（plumbing smoke，非实验结论）：** 修好 `.env` 后，用**本机小模型**在
`988fccb` 上跑了一次 L0（`--max-steps 6 --max-cost-usd 0.05`），验证真实 LLM 路径
端到端成立：物化 → fail-first（退出码 1）→ 工具循环（1 步）→ agent 自报 `completed`
→ 采集 patch → 还原 → 验证（退出码 1）→ `RunResult{outcome: fail, budget_tier: B1,
model: "openai/moa-qwen"}`。**这是一个诚实的 `fail`，不是曲线数据**——它只证明
"链路会用配置里的模型跑、并如实落盘"，不回答剂量曲线问题（小模型 1 步就交终局，
不足以修复）。

**解除阻塞后的 runbook（人工、花钱、不进 CI）：**

一条命令跑完整轮（B1 主实验 → 由 B1 结果推 B3 放宽复跑 → 两份聚合报告）。模型/端点/
key 已在 `.env`，**免费模型按 token 主轴**（§5.3 降级路径）：

```powershell
# B1 每 (任务×档位) 跑 --repeats 次；B3 只对 B1 失败/超预算的档位放宽 token 上限复跑
# （token 基线由 plan_b3_reruns 从 B1 结果推出）+ 落盘 + 报告
.venv\Scripts\python.exe bench\run_curve.py --tasks 988fccb a746b81 --repeats 3 `
  --budget-axis tokens --main-tokens 60000 --relaxed-tokens 180000 `
  --main-steps 30 --relaxed-steps 90
#   → .bench-work/curve/runs/b1/*.json、/b3/*.json、report-B1.json、report-B3.json
#   （报告同时给美元与 token 两列；成本列会全 $0，看 token 列）
```

上限数值待第一条曲线反推：8 次调用烧 57,712 prompt tokens，可据此定 `--main-tokens`。

分步版（想逐步核对时用）：

```powershell
# 1) 指向 API 中档模型（示例：provider/model 按实际凭据填）
$env:LLM_MODEL = "<mid-tier-model>"; $env:LLM_PROVIDER = "<provider>"
$env:LLM_API_KEY = "<key>"; $env:LLM_BASE_URL = "<base-url>"

# 2) B1 主实验：每个 (任务 × 档位) 跑 3 次（§9），每 run 一份 JSON
foreach ($task in "a746b81","0896629","988fccb","bd4f186","60c64ed") {
  foreach ($arm in "L0","L1","L2","L3") {
    foreach ($n in 1..3) {
      .venv\Scripts\python.exe bench\run_task.py --task $task --arm $arm `
        --budget-tier B1 --workdir .bench-work --out ".bench-work/runs/$task-$arm-$n.json"
    }
  }
}

# 3) 聚合出 B1 曲线（离线、零网络）
.venv\Scripts\python.exe bench\report_curve.py --runs .bench-work/runs --tier B1 --out .bench-work/b1.json

# 4) 失败的 B1 档位按 B3 放宽复跑（task/arm 由 b3_candidates 选中），再聚合 --tier B3
```

成本按 §10 粗估：5 题 × 4 档 × 3 次 = 60 个 run，中档模型下十几美元（题集可先减到
2–3 题压到个位数）。归档约定：一 run 一 JSON 落 `.bench-work/runs/`，聚合结果与
markdown 报告落盘，报告里每个数字都能追回具体文件名。
