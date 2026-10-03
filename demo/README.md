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
2. **`demo review` 会取真实的 head sha**（离线通道取自 fixture，真 GitHub 模式向
   GitHub 现取）。曾经这里是"按 (repo, pr) 推导一个假 sha"，那个值只在离线通道下
   成立：接了真 token 之后，webhook 按假 sha 建任务行、worker 从 GitHub 取回真 sha
   再把结论写到真 sha 那一行——库里多出一行，`demo status` 盯着的那行永远停在
   queued。现取真实 sha 同时保住了幂等演示：没有新 commit 时 sha 不变。

## 三件"量出来才知道"的事

### 0. 有一轮"全绿"验的其实是常驻网关（2026-10-03）

本脚本自己起网关在 **8081**，而 `app.cli` 的 `--port` 默认读 `.env` 的
`GATEWAY_PORT`。本机常驻网关一直开着（8083），于是投递全打到常驻那个，本轮起的
演示网关全程闲置——而常驻网关有真 Redis + 真 PG + 同一条队列，worker 又是本脚本
起的，所以**每一条判据都成立**，全绿。

证据只能从访问日志看：常驻网关收到 3 次 `POST /webhook/github/review`，演示网关
收到 0 次。

现在多一条判据专门堵这个：「**投递落在演示网关 (8081) 上，不是常驻网关**」，
它读演示网关自己的访问日志确认那条 POST 出现过。

端口用 `GOLDEN_PATH_PORT` 覆盖；端口上已经有别人在听时脚本直接拒绝跑，不去抢。

### 1. 投递耗时里混进了 `import httpx`（~640ms）

`demo review` 打印的耗时一度是 625~763ms，而用直连客户端打同一个端点是 **19ms**。
差额几乎全在 `import httpx` ——它写在 `_post_webhook` 内部，而整个函数包在计时
区间里。所以那个数字量的是 Python 的 import，不是网关。

修法是把 import 提到计时之前。修完是 153ms。

**这条值得单独记，是因为它正是"把阈值从 1000 挪到 1500 就能通过"的最坏形态**：
阈值本来卡在 968ms，看上去只差一点点，而真正的问题是量的东西不对。发现它的办法
不是调阈值，是拿另一个客户端打同一个端点对一下。

另外脚本显式跑一次 schema 迁移并把网关的 `CODE_REVIEW_AUTO_MIGRATE` 置 0：网关
的第一次建 store 会跑一遍幂等 DDL（实测 ~930ms），算进去量的就不是投递路径了。

### 2. 依赖不在开跑前验，会干等 5 分钟

Ollama 进程中途死掉时，worker 每个 agent 调用都 `ConnectionRefusedError`，任务被判
failed，而脚本还在 `Wait-State` 里轮询——**等了 5 分钟**才报"没在 150 秒内到
waiting_approval"。真正的原因离这句话十万八千里。

所以脚本开头有预检：容器在不在、Ollama 通不通、`qwen2.5:3b` 拉了没，缺哪个当场
说哪个，退出码 2。

### 3. `doctor.py` 之前完全不看 GitHub

`.env` 里 `GITHUB_TOKEN` 从空变成一串字符时，PR 审查链路会立刻"看起来配好了"——
webhook 不再降级、任务照常入队。但过期或复制不全的 token 要到 worker 调 GitHub
API 时才 401，那时投递方早就收到 202 走了。

现在 `doctor.py` 有一项 `github`，探一次 `/user`，把三种情况分开报：离线通道 /
未配置 / 形态对但 GitHub 拒收。

```powershell
uv run python scripts/doctor.py            # 看 github 那一项
uv run python scripts/probe_github_token.py --repo owner/repo   # 只看 token
```
