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

> 2026-11-02 勘误（第 3 行）：原记「被挡=门禁生效证据」，改判「误挡」。验证根本没跑起来
> （副本缺 pytest）、超时上限低于真门禁运行时长、临时目录在 0 字节可用空间的盘上——三者
> 说明的是验证环境缺陷，不能拿来当门禁有效的证据。

> 每行交付完成后补齐 run_id 与结果；失败与 NEEDS_HUMAN 也如实记录。
