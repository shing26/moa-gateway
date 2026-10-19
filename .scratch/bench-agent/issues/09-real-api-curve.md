# 09 — 真实 API 跑一轮四档曲线（人工验收）

**What to build:** 前面全是离线确定性，这一条才是实验本身：用真实中档模型在任务集上把 L0–L3 各跑一遍，产出曲线与一份完整报告——卡住也照实记。花钱、不进 CI。收工时要么给出一条曲线，要么给出"在当前预算下测不出差异"这个同样值钱的结论。

**Blocked by:** 01 — 任务集；06 — L3；07 — 预算策略；08 — 报告

**Status:** **done** — 2026-10-19 用 `nvidia/nemotron-3-super-120b-a12b`（免费端点）
跑完 2 题 × 4 档 × 1 次，B1 八条全部有效、B3 三条补齐，曲线有分化（见文末
「最终曲线」）。**单一重复、仅 2 题**，结论是方向性的，不是统计性的。

- [x] 选定中档模型：**最终**用 `nvidia/nemotron-3-super-120b-a12b`（NVIDIA integrate API，
      `https://integrate.api.nvidia.com/v1`）。已写入 `.env`（`LLM_PROVIDER=nvidia`）；
      实际 `model` 值由 `RunResult.agent_run.model` 如实落盘。是否支持 native
      tool-calling 由首跑验证——不支持会落 `outcome=error`，这本身也是结论。
      （原选型 `nemotron-3.5-super-vl-120b-a12b` 在 A/B 里被同任务的
      lightning-30b 与 step-5-preview 比下去，见文末。）
- [x] L0–L3 在同一任务集、同一 token 预算下各跑完
- [x] 失败的档位按 B3 放宽后再跑一遍，标注增量成本
- [x] 产出曲线 + 完整 `RunResult` 归档
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
  --budget-axis tokens --main-tokens 400000 --relaxed-tokens 1200000 `
  --main-steps 40 --relaxed-steps 120
#   → .bench-work/curve/runs/b1/*.json、/b3/*.json、report-B1.json、report-B3.json
#   （报告同时给美元与 token 两列；成本列会全 $0，看 token 列）
```

数值由校准反推（1 个 run 实测 ~7.6k token/步、~25 s/步）：30 步用掉 237k token，撞的是
**步数**不是 token；所以抬到 40 步、token 留 400k 余量。**一个 run 约 12 分钟**，按 40 步
可能到 17 分钟——题量与重复次数要按这个排（2 题 × 4 档 × 3 次 ≈ 5–7 小时）。

**首轮真实曲线（2026-10-18，1 题 × 4 档 × 1 次，token 主轴）：** `988fccb` 上四档全
没过，但四档的死法不同，而且暴露出一个**真缺陷**：

| 档 | B1 结局 | 调用 | token | 说明 |
|---|---|---|---|---|
| L0 | budget_exhausted | 9 | 66,019 | 侦察到预算耗尽，没走到写修复 |
| L1 | budget_exhausted | 8 | 65,366 | 同上；Critic 回边没轮上 |
| L2 | fail（step_limit） | **1** | 1,206 | **planner 未完成**——见下 |
| L3 | fail（step_limit） | **1** | 1,216 | 同 L2（L3 复用 L2 的 planner） |

**缺陷：L2/L3 的 planner 用一次调用就判死。** `L2PlannerWorkerCritic.__init__` 把
`pool.tools`（全套工具）交给 planner，而 `planner_steps=1`——真实模型第一轮就调工具，
唯一的一步被工具轮次吃掉，`advance` 返回 `step_limit`，整档在**分解阶段**结束，
worker 一次都没跑。前几轮离线测试没抓到它，因为 scripted planner 只会回文本、从不调
工具。修了两处（都在 `bench/arms/l2.py`，L3 继承自动生效）：

1. **planner 不暴露工具**（空 `ToolRegistry`）。`chat_with_tools` 带 `tools=[]`，模型
   只能回文本。
2. **planner 换角色指令**（`planner.PLANNER_INSTRUCTIONS`）。只去掉工具还不够：基础
   system prompt 是编码 agent 的（"先读再看""每一步做一件事"），实测模型会把工具调用
   **写进正文**（`<|open|>argument ... <|call|>`），`parse_plan` 照样读不出来。补一句
   "你是规划者，不探索、不调工具、只回 JSON" 后，同一提示下模型直接回出合法 JSON 数组。

回归用例 `tests/unit/test_bench_arm_l2.py::test_planner_is_offered_no_tools_while_the_worker_gets_them`
钉住"planner 收到 `tools=[]`、worker 收到非空工具表"。修后真实 L2 复跑：planner 产出
4 条子任务（`subtask_count=4`）、worker 真跑了 9 步（list_files / read_file / search /
run_tests），**不再是 1 次调用判死**。

**端点噪声（要记的结论之一）：** 修好 planner 后，L2 复跑仍在 worker 段撞上端点 500
（`litellm.InternalServerError: Failed to generate completions`），落成
`outcome=error`（§8 的噪音腿，不进通过率分母）。planner 那一轮也偶发同样 500。
跑整轮曲线时 error 率会明显 >0；这是**端点的性质**，不是 bench 的判决问题——报告已把
`error` 单独成列。

**预算校准（2026-10-18）：瓶颈不在数值，在 `read_file` 缺分页——已修，修后首个 pass。**

先按"30 步 × ~8k prompt token/步"把 B1 抬到 `--main-tokens 300000` 跑 `988fccb / L0`
校准，结果是 **26 次调用、308k token、patch 0 字节**：模型全程在读，一次 `write_file`
都没调；56 次工具调用里 **18 次被拒**，全是 `read_file(offset/limit)`——工具没声明这两个
参数，它读不到就重读整文件，把预算烧在无效调用上。**加 token 只会让这个循环转更久。**

修 `bench/agent/tools.py` 的 `read_file`：支持按行 `offset`/`limit`，仍受 `read_bytes`
约束（分页不是绕过观察上限的口子），并接受模型实际会发的数字字符串（`"160"`）。
同一配置复跑：**`outcome=pass`**——它读懂了测试、定位到 `app/middleware/auth.py`，补上
`/knowledge` 与 `/api/v1/privacy` 两个前缀（正是任务的修复面），30 步时判 `step_limit`
但判决已通过、`tests_touched=False`。

校准实测（这次是**数值**该用的依据）：

| 指标 | 实测 |
|---|---|
| 步数 / 调用 | 30 / 30（撞步数上限，没撞 token 上限） |
| prompt / completion | 226,913 / 10,531（合计 237,444） |
| 单步 prompt | ~7.6k token |
| 墙钟 | 743 s（**~25 s/步、~12.4 min/run**） |
| 工具分布 | read_file 18、run_tests 9、list_files 4、search 3、write_file 2、edit_file 2（未注册→failed） |

→ 结论：**token 上限不是绑定约束，步数才是**（300k 没用满就撞了 30 步）。参数建议见下；
`edit_file` 是模型幻觉出的工具名（bench 只有五个工具），当前记 `failed`；按 ADR-018 的
口径它更该是 `rejected`（模型自纠得了），留作后续小修。

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

---

## 2026-10-19 换模型 + 两个真缺陷（第一轮 curve2 折了）

### 换模型：`nvidia/nemotron-3-super-120b-a12b`

按"免费端点 + 原生 tool-calling + 面向 coding/agentic"把 NVIDIA 免费模型过了一遍
（`build.nvidia.com` 的 `nim_type_preview` = "Free Endpoint"，共 39 条；先取
SSR 的 model 记录，再逐个打真实端点）。实测（同一把 key，1 次 `get_time`
tool-calling 小请求；括号里是 5 连发的成功/中位延迟）：

| 模型 | tool-calling | 说明 |
|---|---|---|
| `nvidia/nemotron-3-super-120b-a12b` | ✅（5/5，1.1s） | 120B/12B active，1M ctx；模型卡"excelling in agentic reasoning, coding, planning, tool calling" |
| `nvidia/nemotron-3.5-lightning-30b-a3b` | ✅（5/5，0.9s） | 30B/3B active，"Long-running autonomous agents"、sub-agent workhorse |
| `nvidia/nemotron-3-ultra-550b-a55b` | ✅（5/5，1.3s） | 1M ctx，agentic reasoning / coding / planning / tool calling |
| `stepfun-ai/step-5-preview` | ✅（5/5，0.9s） | 600B/27B active，1M ctx，long-horizon tool use、软件工程 |
| `nvidia/nemotron-3.5-super-vl-120b-a12b` | ✅（5/5，0.9s） | 原选型（VL），能力已证（L0 出过 pass），但端点抖动下同款 |
| `meta/muse-glimmer-30b` | ✅（3.9s） | native tool-calling |
| `openai/gpt-oss-20b` | ✅（1.5s） | reasoning，20B |
| `poolside/laguna-xs-2.1` | ⚠️（3/5，58.5s） | "long-horizon agentic coding"，但慢且超时 |
| `deepseek-ai/deepseek-v4.1-flash` | ✅（23s） | 慢 |
| `z-ai/glm-5.3` | ✅（74s） | 753B，冷启动极慢（id 是 `z-ai/glm-5.3`，点号） |
| `google/gemma-4-31b-it` | ❌（60–200s 超时） | 不可用 |

**选定 `nvidia/nemotron-3-super-120b-a12b`**：与已证的 120B 同档（chart 的"中档"
要求），去掉 VL 开销，模型卡逐字写清了 agentic reasoning / coding / tool calling。
模型卡另有一条硬要求：**`temperature=1.0` / `top_p=0.95`**，所以 `.env` 的
`LLM_TEMPERATURE` 从 0 抬到 1.0（`LLMConfig` 没有 `top_p` 字段，留默认）。

### 缺陷 1：一次端点抖动就把整条 run 判死

第一轮真实曲线（`curve2`，旧模型）跑到第三题就没了，两条已完成的 run 都是
`outcome=error`。翻日志：90 次 LLM 调用里 2 次 500（`Failed to generate
completions`），而 `LoopSession.advance` 把**任何** LLM 异常直接落 `error`——
40 步的 run 在 2% 单次失败率下只有约 45% 能活到终点。这不是判决，是噪音。

修：`bench/agent/loop.py` 加 `_llm_turn()` 有界重试（3 次、1s/3s 退避，
`CancelledError` 不重试），耗尽后照旧落 `error`。四个结局的语义一字未改。
用例：`test_llm_failure_is_error` 改成"每次都抛"，新增
`test_transient_llm_error_is_retried_then_completes`。

### 缺陷 2：Windows 上解包偶发 `OSError: [WinError 1450]`

同一次 `curve2` 的崩溃点不是模型，是 `materialize` 里
`tar.extractall(dest, filter="data")`——`filter` 会对每个成员调一次
`os.path.realpath`，Windows 上偶发"系统资源不足"。修：
`bench/harness/workspace.py` 把解包提成 `_extract_archive()`，抖动时清空半成品
目录后重试（3 次、0.5s 起步退避）。

### 现在的跑法

```powershell
.venv\Scripts\python.exe bench\run_curve.py --tasks 988fccb a746b81 --repeats 1 `
  --budget-axis tokens --main-tokens 400000 --relaxed-tokens 1200000 `
  --main-steps 40 --relaxed-steps 120 --workdir .bench-work\d3
```

`workdir` 用短名（`d3`）是为了压路径长度。进度看 `.bench-work\d3\runs\b1\*.json`，
日志 `.bench-work\d3.err.log`。

### 第一轮 B1（`988fccb`，nemotron-3-super-120b-a12b）：有分化

| 档 | outcome | 自报 status | 步数 | token | patch | 备注 |
|---|---|---|---|---|---|---|
| L0 | fail | completed | 7 | 43,201 | 0 B | 自报做完；和 A/B 里同款过早收工 |
| L1 | **pass** | step_limit | 13 | 350,064 | 1,382 B | Critic 回边救回来的 |
| L2 | budget_exhausted | budget_exhausted | 37 | 414,593 | 0 B | 「计划未执行完」 |
| L3 | error | error | 0 | 737 | 0 B | 「planner 未产出可读的计划」 |

L1 是唯一做成的一档，判停是"Critic 连续打回达上限"——同一模型、同一道题，光靠循环
结构就从 fail 翻到 pass，正是这条 bench 要看的形状。

**L3 的 error 是缺陷，不是噪音。** planner 只花了 420 prompt / 317 completion token，
输出读不出计划，`PlanParseError` 直接把整档判死（0 步）。而同一轮 L2 的 planner 是好的
——`temperature=1.0` 下的间歇性格式抖动。`planner_steps=1` 等于一次读不出就陪葬。

已修（`PlanPolicy.planner_attempts=3`）：同一会话里把 `PlanParseError` 原文回灌重问
（`build_plan_retry_message`），耗尽才落 `error`；解析原因也进 `AgentRun.error`
（原来这里是空的）。用例：`test_planner_parse_failure_is_asked_again` 钉"问一次就恢复"，
`test_unreadable_plan_is_an_honest_error` 钉"问满 3 次才认输"。

**已补：** B3 跑完后用修好的 planner 单独补跑了 `988fccb` 的 L3（40 步、4 个子任务、
237,070 prompt + 27,508 completion）。原 `error` 那条留成
`runs/b1/988fccb-L3-1.error.json.bak`，报告只读补跑那条。

---

## 最终曲线（2026-10-19）

模型 `nvidia/nemotron-3-super-120b-a12b`（免费端点，`temperature=1.0`），
2 题 × 4 档 × 1 次，B1 = 40 步 / 400k token，B3 = 120 步 / 1.2M token。
归档 `.bench-work\d3\runs\{b1,b3}\*.json`、`report-B1.json`、`report-B3.json`。

### B1（8/8 有效，零 error）

| 任务 | L0 | L1 | L2 | L3 |
|---|---|---|---|---|
| `988fccb` | fail（7 步 / 43k） | **pass**（13 / 350k） | budget_exhausted（37 / 415k） | fail（40 / 265k，4 子任务） |
| `a746b81` | **pass**（35 / 287k） | **pass**（25 / 146k） | **pass**（40 / 328k） | **pass**（40 / 171k） |

| 档 | 通过率 | 平均 token | 平均每 pass token |
|---|---|---|---|
| L0 | 50% | 165,071 | 330,141 |
| L1 | **100%** | 248,192 | 248,192 |
| L2 | 50%（1 条撞预算） | 371,190 | 742,380 |
| L3 | 50% | 217,867 | 435,733 |

边际：**L0→L1 +50pp / +83k token；L1→L2 −50pp / +123k；L2→L3 +0pp / −153k。**

读法：**L1 是峰值**。同样的模型、同样的题，只加一条 Critic 回边就能把 `988fccb`
从 fail 翻成 pass；再往上加 planner（L2）成本涨了、通过率反而掉回来。L3 的 token
比 L2 低是因为它把活分给并发 worker，但通过率没比 L2 好。**"更多 agent" 在这两题上
没有换来更好的判决**——这是这一轮最有内容的一句话。

### B3（放宽后复跑 B1 里失败的格）

| 格 | B1 | B3 | 增量 token | 结论 |
|---|---|---|---|---|
| `988fccb` / L0 | fail（7 步 / 43k） | fail（9 步 / 69k） | +25,645 | 放宽救不了"过早收工"——是行为问题不是资源问题 |
| `988fccb` / L2 | budget_exhausted（37 / 415k） | **pass**（55 / 523k / 1479s） | +107,980 | 预算确实是瓶颈 |
| `988fccb` / L3 | fail（40 / 265k） | **pass**（77 / 648k / 604s） | +383,634 | 同上；代价是 2.4 倍 token |

L1 与 `a746b81` 四档在 B1 就通过，不是 B3 候选。

### 结论与限制

**结论**：曲线有分化，"结构换能力"在这两题上确实成立，但**只在 L0→L1 这一段**；
L1 之上是净亏损（成本涨、通过率不涨）。L0 的失败是行为层的（模型 7 步就自报完成），
放宽预算无效；L2/L3 的失败是资源层的，放宽就过。

**限制（必须随结论一起引用）**：

1. **`repeats=1`**，报告自己标了 `is_single_sample=true`、每个格子都 `under_sampled`。
   这张表是**方向性证据**，不是统计结论——单个 pass/fail 翻转就足以改变任何一列。
2. **只有 2 题**（E1 共 5 题），且 L2 的 50% 是"1 pass + 1 budget_exhausted"，
   L3 是"1 pass + 1 fail"，n=1 的格子里任何解释都成立。
3. **B3 的 L3 格是手工补跑的**：`run_task.py` 不带 `--baseline-tokens`，事后按 B1
   同格的 264,578 补上（`incremental_tokens` 是派生字段，直接读 `baseline_tokens`）。
4. 补跑的 L3 用的是 `7c0a7e0`（planner 有界重问），其余 7 条用的是它之前的代码。
   改动只影响解析失败的重问，不影响模型与档位行为。

### 过程中修掉的四个真缺陷（都在真实模型下才暴露）

| # | 症状 | 修法 | commit |
|---|---|---|---|
| 1 | L2/L3 的 planner 被工具轮次吃掉，一次调用判死 | planner 用空工具表 + 角色指令 | `0cf5ae9` |
| 2 | `read_file` 不声明 `offset/limit`，预算烧在重读整文件上 | 支持分页，schema 同时收 int/string | `81af9d2` |
| 3 | 一次端点 500 把整条 12 分钟的 run 判死 | `_llm_turn()` 有界重试 3 次 | `a55944f` |
| 4 | planner 一次读不出 JSON 让整档 0 步落 `error` | `planner_attempts=3` + 错误回灌重问 | `7c0a7e0` |

外加一条 Windows 环境问题：`tar.extractall(filter="data")` 偶发
`OSError: [WinError 1450]`（`a55944f` 里一起修了）。
