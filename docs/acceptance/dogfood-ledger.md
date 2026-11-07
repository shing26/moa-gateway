# Dogfood 台账（ADR-033）

受治理交付 Agent 的真实使用记录。**计数规则（预注册，见 ADR-033）**：只有满足
「goal 来自真实需求、仓库真实维护、验证命令是该仓库真实门禁」的交付才计入
「连续 10 个真实交付」终点；引导性/基础设施交付如实标注，不计入。

- 启动日期：2026-10-31
- 终点：连续 10 个真实交付完成全链路（实现 → 验证 → 推送审批 → CI → 合并审批 → 证据对账），
  或自启动起 4 周内凑不满——两个结局都如实记录。
- 判定人：仓库所有者（shing26）；每次交付的推送与合并审批均由人在 TTY/会话中完成。

## 修正案（2026-11-02，首个真实交付入账前修订；预注册期内合法）

1. **计数口径**：「连续 10 个」修订为「累计 10 个」。被门禁挡下的交付如实入账并记为
   门禁生效证据（被挡 ≠ 事故）；**唯一断连/警报事件是「逃逸」**——未经全部审批进入
   远端或主干的变更。修订理由：对治理系统而言门禁生效是产品在工作，不是失败；
   连续口径会把最好的证据排除在终点之外。
2. **goal 先于开工**：入账交付的 goal 必须开工前有记录（issue / 台账候选，带时间戳）。
   开工后补记 = 引导性质，不计入。
3. **交付身份**：agent 以所有者 fine-grained PAT 执行（ADR-034），归因依据
   `MoA-Trace-Id` trailer。

| # | 日期 | run_id | 仓库 / goal | 验证命令（真门禁?） | 结果 | 计入 |
|---|---|---|---|---|---|---|
| 0 | 2026-10-31 | delivery-1793475111869122300（PR #23） | moa-gateway / 建立本台账（ADR-033 要求的引导交付） | 结构断言（非真门禁） | 已合并（PR #23） | 否（引导） |
| 1 | 2026-11-01 | delivery-1793572800000000000（PR #25，trace trailer 落盘） | moa-gateway / main 平台级分支保护上线（goal：issue #24，agent 提报）| CI / test（真门禁；期间两次 BLOCKED 根因诊断：ci.yml UTF-8 BOM、required check 缺 app_id） | 已合并（PR #25），门禁 CLEAN；缺陷修复过程留痕 issue #24 | 是 |
| 2 | 2026-11-01 | dlv-e74dc36b…（budget_exhausted，469,924 tok / 12 calls）、dlv-eefe0b5c…（failed / infra_error：`Max tokens must not exceed 1048576, got 1200000`，0 tok）、dlv-0833c09f…（budget_exhausted，1,017,463 tok / 17 calls） | moa-gateway / 交付链路接入飞书双向桥（goal：issue #26，2026-11-01 10:31 开工前记录） | CI / test（真门禁；三次尝试都没走到验证环节——工具轨迹只有 read_file / search / run_tests，零 write_file，无 bundle） | 三次未成：能力边界（多文件特性超出实现预算，两档预算下同一失败形状），**非门禁拦截**；人接管完成 webhook 方向（PR #29，已合并 2026-11-02），issue #26 仍 OPEN（飞书「批准合并」回写方向未做） | 否（未完成；如实记录能力边界，非「被挡」） |
| 3 | 2026-11-02 | dlv-75263be04f684dc58f62c568989dc7bc（trace delivery-1793590871617192900） | moa-gateway / `.github/dependabot.yml` 三生态周更（goal：issue #30，2026-11-01 20:32 开工前记录） | 配置的 `python -m pytest -q`（**范围与真门禁不符**：ci.yml 实为 `uv run pytest tests/unit -v`）；临时验证副本缺 pytest → exit 1，`No module named pytest` | NEEDS_HUMAN / verify_failed：agent 31,377 tok / 9 calls，第 3 步就写入正确文件（396 字符），随后 run_tests 一次即收尾；门禁 fail-closed 未产 bundle，未进入推送审批、未推送任何东西 | **否（误挡）**——不是门禁生效证据：三个环境缺陷叠加（① 裸名 `python` 在 Windows 上按父进程 PATH 解析，落到 uv 基础解释器；② 300s 超时低于 `tests/unit` 实测 578s；③ `TEMP` 指向已满的 C: 盘）。2026-11-02 已修，待重跑补记 |
| 4 | 2026-11-04 | dlv-dec44d38eed14071b1f9b4d3e2a757d2（trace delivery-1793784552418262500，PR #35） | moa-gateway / `.github/dependabot.yml` 三生态周更（goal：issue #30，2026-11-01 20:32 开工前记录；第 3 行误挡后的重跑） | 任务级 YAML + 期望键断言（exit 0）；CI / test（真门禁，1m31s pass） | 已合并（PR #35，merge `ab6acfbe`）；5 calls / 18,698 tok / 4 tool calls，轨迹 list_files → write_file → git_diff → read_file；推送与合并审批均由 shing26 在 TTY 完成 | 是 |
| 5 | 2026-11-05 | dlv-eea39153fcf240e89966b264572f1b78（trace delivery-1793854820706907000，PR #48） | moa-gateway / CI 增加 Docker 手动构建触发（goal：issue #43，2026-11-05 开工前记录） | 任务级 `ci.yml` 断言（`workflow_dispatch` 布尔输入、docker 作业 `if`、tag-only `push`）+ README 关键锚点保留；CI / test（真门禁，PR #48 绿）；合并后在 main 手动触发 `run_docker=true`，docker 作业构建成功且 ghcr 登录按设计 skip | 已合并（PR #48，merge `7c8fa231`）；8 次运行、7 次失败全部记录在案（逐次见下方注）。推送成功后本机 PostgreSQL 5433 停机使合并登记失败，按 `recover` 路径补登记、未重推，随后完成人审合并（shing26）；运行记录如实停在 `needs_human` / `infra_error` | 是 |

> 第 3 行的误挡已由第 4 行闭环：同一 issue #30，验证环境修复后一次通过，并完成推送、CI、合并审批与证据对账。

> 2026-11-02 勘误（第 3 行）：原记「被挡=门禁生效证据」，改判「误挡」。验证根本没跑起来
> （副本缺 pytest）、超时上限低于真门禁运行时长、临时目录在 0 字节可用空间的盘上——三者
> 说明的是验证环境缺陷，不能拿来当门禁有效的证据。

> 2026-11-05（第 5 行）：issue #43 共 8 次运行，按 `started_at` 排序——
> `dbb0ea08`、`f024a0df`：LLM 请求 120s 超时（`LLM_TIMEOUT` 未调大，infra）；
> `3f743bab`：bundle 通过但整文件重写改坏 README，人审拒推（→ PR #44 加覆盖上限）；
> `9d73460e`：40 步预算耗尽（→ PR #45 修行号前缀与 `search` 收文件路径）；
> `136442b2`：推送 403，异常只有 URL（→ PR #46 带出 GitHub 原话）；
> `29767b5b`：推送 403 带出「Resource not accessible」，仍不知缺哪项（→ PR #47 指出 Workflows: write）；
> `dace30b5`：待推送批准时终端进程已退出，未推送；
> `eea39153`：11 calls / 70,649 tok 内完成实现与验证，推送成功，随即 PostgreSQL 停机（见下条环境事件）。
> 前 2 次 infra 超时与 README 改坏分属不同层：一个是环境配置，一个是工具写局部性。

> 2026-11-05 环境事件（非门禁）：① 交付用的细粒度 PAT 缺 `Workflows: Read and write`，任何修改
> `.github/workflows/` 的 tree 都返回 403——两个 token 各撞一次，最终由仓库所有者重建并授权；
> ② 推送成功后本机 PostgreSQL 5433 停机，合并登记失败（PR 已真实存在），按 `recover` 路径补登记、
> 没有重推。因此该运行的记录停在 `needs_human`，而合并通道记录为 `merged`（`7c8fa231`）。

| 6 | 2026-11-05 | dlv-569c901db52746ea88811e91cd10674f（PR #52，merge `10f21243`） | moa-gateway / 交付运行在 recover 后与合并通道对账：`needs_human` 可折叠到 `merged`（goal：issue #50，2026-11-05 开工前记录） | 任务级 `python -m pytest tests/unit/test_delivery_store.py tests/unit/test_delivery_state.py -q`（33 passed，基线 29 + 新增 4）；CI / test（真门禁，PR #52 绿） | 已合并（PR #52，merge `10f21243`）。3 次运行：第 1 次 40 步预算耗尽（重新读 500 行测试文件，未写到测试）；第 2 次 70 步耗尽——`run_tests` 的 `-k "a or b"` 被按空白切碎、pytest 报 "file or directory not found: or"，8 步耗在同一条命令（→ PR #51 修引号切分）；第 3 次 36 calls / 253,914 tok 通过，人审推送（shing26，TTY）+ 人审合并（shing26，MergeExecutor） | 是 |

> 2026-11-05（第 6 行）：这个 goal 修的正是第 5 行留下的矛盾——`needs_human` 的运行
> 不能前进到 `merged`。交付本身按同一治理链路完成，落地后第 5 行那种"运行记录停在
> needs_human、合并通道记录为 merged"的运行可以用折叠对账：状态前进、`failure`
> 原样保留、审计带 `folded_from`。附带产出：PR #53（交付前能力自检，五项检查
> ok/warn/blocked，把 issue #43 两次"实现+验证全跑完才被 403 拒"的最贵诊断提前）；
> PR #54（`write_file` 整文件覆盖后在观察结果里报告删除了多少行——第 5 行那次只差
> 37 行 / 约 3%，任何阈值都抓不住，改为把代价摊开给模型与人审）。注释与行为不一致
> 的收尾见 PR #55。

| 7 | 2026-11-05 | dlv-b5a92fcc（40 步 / 405,915 tok，0 写入）、dlv-e2e63201（40 步 / 354,564 tok，8 处写入 + 47 测试通过后差测试编写）、dlv-039c4e7f（16 步假完成，no_changes）、dlv-18bd7a53（60 步 / 576,576 tok，无限重读 0 写入）（issue #59） | moa-gateway / `delivery run` 开工前自动跑 preflight，blocked 即终止（goal：issue #59，2026-11-05 开工前记录；中途按轨迹归因 descope 一次，见 issue 评论） | 任务级 `python -m pytest tests/unit/test_delivery_cli.py tests/unit/test_delivery_preflight.py tests/unit/test_delivery_flow.py -q`（第 2 次尝试中 47 passed） | 4 次尝试未成，**负结论**：非门禁拦截、非环境缺陷（本轮无工具层缺陷），是单 implementer 在横跨状态机/审计/CLI 的多约束任务上行为方差过大——四次失败形状各异（上下文烧尽 / 差测试步数 / 假完成 / 无限重读），而第 2 次已证明任务本身可完成。goal 转人工 | 否（未完成；能力边界如实记录，goal 未关闭） |


> 2026-11-05（第 7 行）：第一个"重跑也不指望变好"的负样本。第 2 行的三次失败
> 后人接管完成了一半；这一次四个失败形状互不相同、且第 2 次证明可完成，说明
> 约束不在任务而在模型稳定性。处理方式照 ADR-033：负结论入账，goal 转人工，
> 不加预算硬凑（第 4 次已是 60 步 / 700k 档，仍然 0 写入——预算不是约束）。
>
> 当日下午起端点侧 agent 行为进一步退化：第 8 个交付（issue #62，goal 已缩到
> 两处 detail 键）连续两次"只读不写"耗尽——3.5-super 与 3-ultra-550b 同形状
> （`dlv-068dfed9`、`dlv-b6f61efb`），而同日早间同模型 36 步完成过更大的任务。
> 结论记在 issue #62：疑似端点侧行为漂移，与任务/goal/模型选择无关。台账推进
> 暂停至端点恢复；负结论与恢复时间本身是对外叙事的一部分。

> 每行交付完成后补齐 run_id 与结果；失败与 NEEDS_HUMAN 也如实记录。

| 8 | 2026-11-06 | dlv-b44faac50e8c46799dd883e33592740d（PR #65，merge `55b94869`） | moa-gateway / preflight report 进审计 detail：blocked 路径加键、放行路径经 `_move` 的 `extra` 参数（goal：issue #62，2026-11-05 开工前记录；两次"只读不写"端点退化后的第三次尝试） | 任务级 `python -m pytest tests/unit/test_delivery_cli.py tests/unit/test_delivery_preflight.py tests/unit/test_delivery_flow.py -q`（exit 0）；CI / test（真门禁，PR #65 绿，2m12s） | 已合并（PR #65，merge `55b94869`）。1 次运行：23 步 / 22 calls / 167,821 tok / 530s，无修订轮一次通过；推送与合并审批均由 shing26 完成（TTY / MergeExecutor） | 是 |

> 2026-11-06（第 8 行 + 端点挑选）：第 7 行注释记的端点退化有了定位结果。对
> build.nvidia.com 免费模型做行为探测（模拟 harness 消息流 + tools schema，
> 观察截断翻页行为）：53 个候选多数 404（Function not found for account），
> 可用的里 reasoning 系（mistral-large、nemotron、deepseek 等）在看到截断观察
> 时倾向继续翻页而非写——第 4 次尝试的 mistral-large 已实证能发合法 tool_calls
> 但 0 写入。**gemma-4-31b-it 是唯一在截断观察下直接发写操作的模型**，换它后
> 一次通过（此前同任务两次 0 写入耗尽）。经验：端点模型列表 ≠ 可用；模型行为
> 差异在"读够了吗"这个判断上，不在工具调用能力上。台账推进按用户指示不再等
> 每周两个的节奏。

| 9 | 2026-11-06 | dlv-64c14f70（infra：端点瞬时过载，0 tok）、dlv-bfd15f75（15 步 / 128,446 tok，4 处写入 + 旧范围验证 exit 0，**未推送——人审闸门拦截**）、dlv-cebae2b20b24454a9ee3021160da0de3（PR #68，merge `8ef7018b`） | moa-gateway / read_file 截断提示带总行数与剩余行数，让模型能判断再翻一页值不值（goal：issue #67，2026-11-06 开工前记录） | 任务级 `python -m pytest tests/unit/test_bench_tools.py -q`（66 passed）；CI / test（真门禁，PR #68 绿，2m6s） | 已合并（PR #68，merge `8ef7018b`）。成行运行 1 次：15 步 / 14 calls / 128,526 tok，4 处写入；推送与合并审批均由 shing26 完成（TTY / MergeExecutor） | 是 |

> 2026-11-06（第 9 行）：goal 直接来自第 7 行负结论的归因——截断提示只说"继续读
> 请用 offset=N"不带总行数，模型无从判断"再翻一页值不值"，默认再翻；这是对
> 无限重读形状的最小工具侧回应。三次启动两个教训：
> ① launcher 的验证命令是上一单遗留（delivery 测试），第二次重跑的 bundle
> 轨迹完美但验证不覆盖改动文件——**推送审批闸门前被人拦下未推**，验证范围
> 错误在这一层被兜住（闸门设计按预期工作）；修正 launcher 后第三次通过。
> ② 端点瞬时过载（Service temporarily overloaded）起步即失败，属 infra，
> 不构成失败形状。交付本身一次通过：15 步 / 4 处写入，gemma-4-31b-it。


| 10 | 2026-11-08 | dlv-c72fc65a（infra：单次调用 300s 超时）、dlv-772162d9（infra：900s 超时，单次调用可无限挂起）、dlv-f0dcd3ad（60 步 41 calls 全部只读零写入）、dlv-0f799136（mistral-large 大 prompt 两次 180s 超时后循环卡死，会话重启终止）、dlv-87419c6f（laguna-xs 100 calls / 1.6M tok 全读零写）、dlv-b86a595（gemma 每调用 120s 超时，fallback nemotron 写出两文件后随会话重启终止）、dlv-333e70b6（nemotron 主选 104 读零写）、dlv-30872304（9 处写入后 120 步上限）、dlv-dfba8210（8 处写入 + 6 轮验证后陷入 `_GitHub` 存根搜索螺旋，200 步上限）、dlv-0e9f6389（17 处写入 + 15 轮验证，3.5M tok 超限，284 步）、dlv-79db8a3d（PR #72，merge `ec52e7f3`） | moa-gateway / 验证命令与改动文件的一致性检查：改动含测试文件而验证命令未引用时，prepare_bundle 在 VERIFIED 分支记 `verify_covers_changes=False` + `uncovered_files` 并在 message 附警告；flow 把 preparation.message 带进推送审批审计 detail（goal：issue #70，2026-11-06 开工前记录） | 任务级 `python -m pytest tests/unit/test_change_pipeline.py tests/unit/test_delivery_flow.py -q`（64 passed, 1 skipped；合并后本地复核同）；CI / test（真门禁，PR #72 绿） | 已合并（PR #72，merge `ec52e7f3`）。成行运行 1 次：114 步 / 108 calls / 1,139,427 tok / 22.9 分钟，20 处 replace_in_file + 12 轮验证；推送与合并审批均由 shing26 完成（TTY / MergeExecutor） | 是 |

> 2026-11-08（第 10 行）：goal 来自第 9 行实证的缺陷——验证命令陈旧时闸门兜住了
> 但成本是整次实现预算；把"命令引用到改动测试文件"变成 prepare 时的机器检查。
> 本行的真实故事是收敛而非实现：四个文件（含 1426/798 行的两个测试文件）对
> 端点现有模型池太重，15 次启动中 10 次在"无限重读"上耗尽步数或 token 预算，
> 其余为 infra（超时 / 429 / 内容安全）。两个有效干预：① goal 写明工作顺序
> （先读两个源文件 → 先编辑 → 测试文件只读头尾、存根自己内联定义）；② 预算
> 上限逐级抬高（60→400 步、700k→5M tok），最后 tokens 仍是约束（1.14M / 5M
> 完成）。dogfood 结论：工具循环不给模型剩余预算信号、不阻止重复读同一文件，
> 是下一行候选缺陷；模型收敛运气占成行成本的大头。合并登记一次 infra 失败
> （Postgres 5433 未启动），按预案 `recover()` 重注册、未重复远端写入。
