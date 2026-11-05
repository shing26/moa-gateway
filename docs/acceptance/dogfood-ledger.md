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

