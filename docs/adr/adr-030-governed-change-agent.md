# ADR-030: 受治理的变更 agent——写侧只在验证与审批之后出口

日期：2026-10-25
状态：已实施（dry-run 垂直切片；真实推送默认关闭）
关联：ADR-021（合并审批）、ADR-027（工作区写隔离）、
`apps/code_review_pipeline/agents/`、`app/agents/workspace_tools.py`

## 背景

`app/` 已经能拦截、审批、留痕、回归；`apps/code_review_pipeline` 能读 PR、
用真实 LLM 审查、产出候选 finding。但整条链路**只有读侧**：

- `GitHubClient` 有 `get_pr / get_pr_files / download_archive / list_reviews /
  create_review / get_ci_status / merge_pr`，**没有**建分支、建 commit、开 PR。
- 全仓 `app/` 与 `apps/` 内搜不到任何 `git commit` / `git push`。
- `workspace_tools.write_file`（`app/agents/workspace_tools.py:219`）能把改动写进
  沙箱，`run_tests`（`:281`）能验证，但**改动出不了工作区**。

结果是 agent 只能"评论别人的代码"，不能"交出自己的改动"。用户真实流程里
（写代码 → 提 PR → 等 CI → 手动合并）最耗时的两段恰好都在写侧。

## 决策

1. **写侧是独立管线，不是给审查链路加分支。** 新增
   `apps/code_review_pipeline/change/`：输入（issue 文本或 CI 失败日志）→ 物化
   工作区 → agent 改代码 → 验证 → 产出**变更包（ChangeBundle）**。
2. **变更包是纯数据，不含副作用。** 它记录 base sha、分支名、提交信息、
   文件级 diff、patch 哈希、验证命令与退出码。生成变更包的过程不触碰远端。
3. **出口只有两条，且顺序固定：验证 → 审批 → 推送。**
   - 验证失败（verify_command 非零、无改动、patch 为空）直接终止，不生成可推送的包。
   - 推送前必须有过审批记录；未审批的包只能停在 dry-run。
4. **dry-run 是默认。** `CHANGE_AGENT_PUSH_ENABLED` 默认 false。默认路径下
   管线只产出变更包与 diff，**不建分支、不建 commit、不开 PR**。显式开启且
   审批通过才走远端写入。
5. **远端写入只做三件事**：`create_ref`（分支）、`create_commit`（Git tree +
   commit）、`create_pull_request`。不删除分支、不 force push、不直接改目标分支。
6. **复用既有闸门，不新造。** 审批走 ADR-021 的合并审批通道与飞书卡片；审计走
   既有 WAL 链；Run Record 记录变更包哈希、验证结果与审批人。
7. **CI 排障与 issue 修复合流。** 两者只差输入采集器：CI 排障多一步读取失败
   job 的日志与 commit sha，之后走同一条写侧管线。不为第二个场景再建一套。
8. **模型质量是显式前提。** 本机 `qwen2.5:3b + 512` 已被实验证明产不出可执行
   计划（v6/v7/v8、分诊 route 38%），不得作为写侧默认后端；写侧要求显式配置
   强模型，未配置时管线以 `model_not_configured` 终止而不是静默降级成规则拼装。

## 替代方案

- **直接在审查链路里加写回**：审查是只读、幂等、可重跑；写是外部副作用，
  混在一起会让"重跑一次审查"变成"重复推送"。否决。
- **让 agent 直接 `git push`**：token 会进命令行与进程环境，且绕过审批与审计。
  否决。
- **先实现 CI 排障再补写侧**：排障的产出仍然要写回，写侧是共同前置。顺序反了
  会做两遍。否决。
- **默认开启推送、靠 branch protection 兜底**：保护分支只挡得住合并，挡不住
  垃圾分支与错误 commit 污染仓库。否决。

## 后果

- 写侧上线后，仓库会出现"由 agent 创建的分支与 PR"。因此分支名、commit message
  必须带可追溯标记（trace_id / 变更包哈希），便于批量识别与清理。
- dry-run 默认意味着**默认部署仍然不会推送**。这会把"能写"和"在写"分开：
  演示与测试只验证到变更包，真实推送需要显式开关加人工审批。
- 验证命令由部署方提供（`CHANGE_AGENT_VERIFY_COMMAND`）。没有配置验证命令时，
  管线拒绝产出可推送的包——"没验证过就推"不在允许范围内。
- 变更包只保存 diff 与哈希，不保存完整仓库快照；重建依赖受控复跑。

## 落地记录（2026-10-25）

在私有仓库 `shing26/aircontrol-next` 上跑通真实写侧（探针 PR 已关闭、分支已删除，
仓库恢复原状）：

- 变更包 → 验证通过 → 审批 → `create_ref` → `create_commit` → `create_pull_request`
  全部成功，PR #1 状态 open、`mergeable=true`、1 文件 +6 行、commit message 与
  base sha 均正确。
- **真环境暴露了一个单测发现不了的缺陷**：首次推送用本地临时仓库的 HEAD 当
  `base_sha`，GitHub 返回 `422`，而错误信息看起来像权限或分支问题。修复为推送前
  用 `commit_exists` 校验远端存在性，并给出明确报错；新增回归测试
  `test_push_rejects_base_sha_missing_from_remote`。
- 该 token 缺 `Checks: read` 权限，`get_ci_status` 返回 403。这与写侧无关，但意味着
  合并通道在当前 token 下读不到 CI 结论；需要补权限后才能跑通 ADR-021 的完整闭环。
