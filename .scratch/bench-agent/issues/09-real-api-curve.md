# 09 — 真实 API 跑一轮四档曲线（人工验收）

**What to build:** 前面全是离线确定性，这一条才是实验本身：用真实中档模型在任务集上把 L0–L3 各跑一遍，产出曲线与一份完整报告——卡住也照实记。花钱、不进 CI。收工时要么给出一条曲线，要么给出"在当前预算下测不出差异"这个同样值钱的结论。

**Blocked by:** 01 — 任务集；06 — L3；07 — 预算策略；08 — 报告

**Status:** blocked — 缺真 API 凭据与中档模型选型

- [ ] 选定中档模型，记录选型理由与 `RunResult` 里的实际 `model` 值
- [ ] L0–L3 在同一任务集、同一预算下各跑完
- [ ] 失败的档位按 B3 放宽后再跑一遍，标注增量成本
- [ ] 产出曲线 + 完整 `RunResult` 归档
- [x] 报告里每个数字都能追到具体的 run（离线聚合已就绪，见 08）
- [x] 不把结果并进 CI（`bench/run_task.py` / `bench/report_curve.py` 都不在 `tests/`）

**为什么卡住：** 这台机器的 LLM 配置指向**本机小模型**，不是 §12 要的 API 中档模型。
`.env` 里 `LLM_PROVIDER=local` / `LLM_BASE_URL=http://localhost:11434/v1` /
`LLM_MODEL=moa-qwen`，`OPENAI_BASE_URL` 同样指向 `localhost:11434`；没有云端 API 凭据。
§13/§12 明确"本机小模型不参与 bench""模型走 API 中档"，所以**不能用它跑曲线**——照跑
只会得到"所有档位都失败"的假结论，正是 §8 要避免的噪音。解除阻塞需要：一个 API 中档
模型的 key + 模型名（写进 `LLM_*` 或 `--model`）。

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
