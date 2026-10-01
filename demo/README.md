# golden path（D5）

一条命令跑完"PR 审查"这条链路的全部可靠性判据：

```powershell
docker compose -f docker-compose.dev.yml up -d redis postgres
pwsh -File demo\run_golden_path.ps1
```

需要本机有 Ollama 且已拉 `qwen2.5:3b`（脚本用它跑 5 个 agent 的真实推理）。

## 它验了什么

| 步 | 判据 |
|---|---|
| 2 | 投递 **<1s 返回 202**，不在请求里跑 agent（worker 故意没起） |
| 3 | 同一 (repo, pr, sha) 重投 → **200 idempotent**，任务行数不变 |
| 4 | 起 worker → 5 个 agent 真跑 → `waiting_approval` |
| 5 | dry-run 批准**零副作用**：状态不动、不记 id、不写审计 |
| 6 | 真批准 → PR 上恰好 1 条评论 |
| 7 | 再批准一次 → `already_posted`，评论数仍是 1 |
| 8 | 审计链种类齐全，且本轮新增 9 条 |
| 9 | `kill -9` worker → 任务卡在 `running` → 重启后经 `XAUTOCLAIM` 接管并跑完 |
| 10 | 崩溃恢复后的任务审计同样完整 |

## 手工命令

```powershell
$env:GITHUB_WEBHOOK_SECRET="demo-secret-abc"
$env:CODE_REVIEW_GITHUB_FIXTURE="demo/fixtures/github_pulls.json"

uv run python -m app.cli demo review 42     # 202
uv run python -m app.cli demo review 42     # 200 idempotent
uv run python -m app.cli demo status "shing26/moa-gateway#42@demo0000000000000000000000000000000dead"
uv run python -m app.cli demo approve "shing26/moa-gateway#42@demo0000000000000000000000000000000dead"
```

`demo review` 走**真实 HTTP + 真实 HMAC 签名**，不直接调内部函数——直接调会跳过
验签、幂等与入队，那样得到的"成功"证明不了任何事。

## 关于离线 GitHub 通道

`CODE_REVIEW_GITHUB_FIXTURE` 会把 GitHub API 换成读本地 fixture、写
`data/demo_reviews.json`（默认 `CODE_REVIEW_GITHUB_FIXTURE_STORE`）。开启时 worker
会打一条 warning，说明评论**不会**到真实 PR。

被替掉的只有"去 api.github.com 拉 diff"和"在 PR 上贴评论"这两件必须联网的事；
验签、幂等、队列、认领、崩溃恢复、状态机、审计、写回判定全走真实路径。所以它验证
的是这套底座，不是 GitHub。

`list_reviews` 读的是上一步 `create_review` **真实写下去**的 JSON，所以第 7 步
"重复审批只发一条"在离线模式下是照样被验到的，不是演出来的。

## 判据里与计划不一致的两处

1. **审计是 9 条不是 7 条**。计划假设 lifecycle 只有 1 条，实际上有 3 条
   （`request_approval` / `posting` / `complete`）。原先 `publish_review` 推进
   `done` 时**没写审计**，于是审计链里"写回成功"与"在 posting 被杀掉"长得一模一样
   ——已补。
2. **`demo review` 的默认 sha 按 (repo, pr) 稳定推导**，所以同一条命令连跑两次
   能直接验幂等。想跑新任务就换 PR 号，或 `--sha`。

