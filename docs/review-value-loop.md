# PR Review 价值闭环

## 固定对比

同一模型、`CODE_REVIEW_TEMPERATURE=0`、相同 token 与 workspace 预算下依次跑：

| 路径 | 拓扑 |
| --- | --- |
| `R0` | 当前五段审查，无 Critic |
| `R1` | R0 + Review Critic keep/revise/drop |
| `R1+I` | R1 + 一份预声明改进 |

runner 按 case 累计 `CODE_REVIEW_EVAL_TOKEN_BUDGET`，超限立即失败；并用
`CODE_REVIEW_EVAL_WORKSPACE_BUDGET_SECONDS` 给整个审查 case 设置硬超时。实际调用过
的任何模型只要不等于配置的 `CODE_REVIEW_MODEL`（包括内部 fallback），该 run 直接
失败，不允许把混合模型结果写成同一实验。

固定真实语料在 `evals/datasets/review_pr_corpus.json`：四个公开仓库各 6 个 PR，
每个库小/中/大三档各 2 个，base/head SHA 已锁定。第三方 PR 只离线读取，不写回。

受控 recall 在 `evals/datasets/review_controlled_cases.json`：5 条 E1、7 条 E3，
每条只有一个已知缺陷；`input_redaction` 明确禁止把 test path、solution patch 或
题面说明放进审查输入。`controlled_review_input()` 会从 test patch 中移除测试文件和
所有 `tests/` diff，只暴露 `case_id`、来源、base SHA、由 base 与 patches 内容派生的
稳定 `head_sha`，以及注入后的源码 diff。

## 指标

- factual precision：人工标为有效（可行动或非行动）的 finding / 已人工标注 finding
- actionable acceptance：`accepted_actionable` 占比
- duplicate / already-known rate
- controlled recall：固定缺陷被发现的条数 / 已知缺陷总数
- evidence coverage：`evidence_verified=true` 的发布 finding 占比；该 flag 只能由
  diff/workspace/tool-output 解析器通过后产生，不能靠字符串形状自报
- cost / PR、latency / PR、Critic overhead、revision/drop rate

受控 case 的 `known_defects_found` 必须由唯一人工终审者对照该 case 的离线答案填写；
runner 不会根据 finding 文本自动推断命中，避免把模型自评写进 recall 分子。

模型预标保存在 finding 的嵌套 `model_prelabel`，不计入人工 label completion，
也不用于 final precision。人工终审是唯一完成条件。配置
`CODE_REVIEW_EVAL_GRADER_MODEL` 时，runner 用独立调用生成预标；未配置时 finding
保持 `disposition=""`、`label_source=""`，不会伪装成已经人工审核。

runner 导出全部 persisted lineage：candidate、superseded、published、dropped 都会
保留 `status`、`published`、`critic_decision`、`revision_of` 和来源 Agent。指标只在
`published=true` 的 finding 上计算；revision/drop rate 按唯一候选 UID 计分母，避免
同一候选的多个快照被重复计数。

## 报告

先分别运行 R0、R1；完成人工终审并选定唯一改进后，再运行 R1+I：

```powershell
.venv\Scripts\python.exe scripts\run_review_value.py --arm R0 --output evals\results\review-runs.json
.venv\Scripts\python.exe scripts\run_review_value.py --arm R1 --output evals\results\review-runs.json
.venv\Scripts\python.exe scripts\run_review_value.py --arm R1+I --improvement-code LOW_PRECISION_EVIDENCE_GATE --changed-path apps\code_review_pipeline\agents\report_agent.py --improvement-base-ref <R1-run-commit> --output evals\results\review-runs.json
```

runner 使用锁定的 24 个 PR 和 12 个受控 case，强制 temperature 0，并把每个 arm
的 findings、成本、延迟、base/head SHA 写成同一份 run record。受控 case 只把
`controlled_review_input()` 返回的源码 diff 交给审查链路，不暴露测试文件、
solution patch 或题面说明。模型输出只作为待审 finding，`disposition` 留空，
必须由人工逐条终审后才能进入报告分母。R1+I 还必须给出 R1 的 commit 作为
baseline；runner 会拒绝“相对 baseline 不是恰好一个 changed path”的运行，防止
把多处修改混成一个改进。

人工终审时再把顶层 `disposition`、`reason_code`、`label_source="human"` 写入同一
finding；下面展示的是 runner 刚生成、只有模型预标的格式：

```json
{
  "runs": [
    {
      "case_id": "fastapi-16535",
      "arm": "R1",
      "findings": [
        {
          "finding_uid": "...",
          "status": "published",
          "published": true,
          "revision_of": null,
          "critic_decision": "keep",
          "evidence_verified": true,
          "disposition": "",
          "reason_code": "",
          "label_source": "",
          "model_prelabel": {
            "disposition": "accepted_actionable",
            "reason_code": "",
            "model_id": "independent-grader-model"
          }
        }
      ],
      "known_defects_found": [],
      "known_defects_total": 0,
      "cost_usd": 0.0,
      "critic_cost_usd": 0.0,
      "latency_ms": 0,
      "model_id": "same-configured-model",
      "temperature": 0,
      "token_budget": 12000,
      "workspace_budget_seconds": 300,
      "base_sha": "2e13450ea624cafd69a528747d3b7272f2ab1c3d",
      "head_sha": "d937d9161da3744fc4fee18fb1a44398b39cd848",
      "improvement_code": "",
      "changed_paths": []
    }
  ]
}
```

然后运行：

```powershell
.venv\Scripts\python.exe scripts\review_value_report.py path\to\runs.json --json path\to\report.json --markdown path\to\report.md
```

报告用第一个命中的改进规则，不允许多项同时改动而污染归因。
- 报告会加载 24 个 PR 与 12 个受控 case 的 manifest；任一 arm 缺 case、重复 case
  或多出未登记 case 都直接失败，不能只提交成功样本。
- 报告同时校验三条 arm 的模型、temperature、token/workspace budget 完全一致，
  并逐 case 校验每条 arm 使用相同、且与 manifest 完全一致的 base/head SHA；协议漂移
  会直接失败。
- `R1+I` 每条 run 必须声明与首轮指标选出的 `selected_improvement` 相同的
  `improvement_code`，并且只能有一个 `changed_paths`；该路径必须在全部 `R1+I`
  case 中一致。R0/R1 若声明改进来源同样直接失败。

## 当前状态

仓库目前提交的是语料、可执行 runner、人工标注契约和报告器，**尚未产生
R0/R1/R1+I 的质量结论**。完成真实对比仍需要配置同一中档模型、运行 36 个 case
的三个 arm、逐条人工终审输出 finding 并导出 run records；在此之前 README
不宣称 Critic 或反馈迭代带来正向收益。
