# golden path 端到端验收（D5）。一条命令跑完全部判据，失败即非 0 退出。
#
#   pwsh -File demo\run_golden_path.ps1
#
# 之所以做成脚本而不是一串手敲命令：这套判据里有三处**只能靠观察时序**才能成立
# ——投递必须在 worker 起来之前返回 202、崩溃必须发生在"已认领、未落库"这个窗口、
# 重投必须发生在任务完成之前。手敲时每一步的先后都靠人记。

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$TaskKey = "shing26/moa-gateway#42@demo0000000000000000000000000000000dead"
$Failed = @()

function Step($name, [scriptblock]$body) {
    Write-Host ""
    Write-Host "== $name" -ForegroundColor Cyan
    try {
        & $body
    } catch {
        $script:Failed += $name
        Write-Host "   FAILED: $_" -ForegroundColor Red
    }
}

function Assert-True($cond, $msg) {
    if (-not $cond) { throw $msg }
    Write-Host "   ok: $msg" -ForegroundColor DarkGray
}

# ── 环境 ────────────────────────────────────────────────────────────────
# 演示走**离线 GitHub 通道**：不碰真实 PR、不发真实评论。底座（验签/幂等/队列/
# 认领/崩溃恢复/审计/写回判定）全走真实路径，被替掉的只有 GitHub API 本身。
$env:GITHUB_WEBHOOK_SECRET = "demo-secret-abc"
$env:CODE_REVIEW_GITHUB_FIXTURE = "demo/fixtures/github_pulls.json"
$env:CODE_REVIEW_GITHUB_FIXTURE_STORE = "data/demo_reviews.json"
$env:CODE_REVIEW_MODEL = "qwen2.5:3b"
$env:CODE_REVIEW_FALLBACK_MODEL = ""
$env:CODE_REVIEW_FALLBACK_API_KEY = ""
$env:CODE_REVIEW_AUTO_MIGRATE = "1"
$env:PYTHONUNBUFFERED = "1"
# 崩溃恢复的感知延迟。生产默认 60s，这里调到 4s 是为了让"崩了多久能恢复"这条
# 判据在一轮 demo 里跑得完——不改默认值，只把这个折中调小。
$env:TASK_RECLAIM_MIN_IDLE_MS = "4000"

function Psql($sql) {
    docker exec moa-gateway-postgres-1 psql -U gateway -d gateway -t -A -c $sql
}

function Start-Proc($argsList, $log, $err) {
    # **直接跑 venv 的 python，不经 `uv run`**。`uv run` 会 fork 出一个子 python，
    # 于是 Stop-Process 杀掉的是 uv 自己，真正的 worker 继续活着——第 9 步的
    # "kill -9" 就成了假的：任务其实没经过崩溃恢复。
    Start-Process -FilePath "$Root\.venv\Scripts\python.exe" -ArgumentList $argsList `
        -WorkingDirectory $Root `
        -WindowStyle Hidden -RedirectStandardOutput "$Root\demo\$log" `
        -RedirectStandardError "$Root\demo\$err" -PassThru
}

function Wait-Http($url, $seconds = 40) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try { return Invoke-RestMethod $url -TimeoutSec 5 } catch { Start-Sleep -Milliseconds 700 }
    }
    throw "gateway 未在 $seconds 秒内就绪：$url"
}

function Wait-State($want, $pr, $seconds = 120) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        $s = (Psql "SELECT status FROM code_review_prs WHERE pr_number=$pr").Trim()
        if ($s -eq $want) { return $s }
        Start-Sleep -Seconds 2
    }
    throw "PR #$pr 没在 $seconds 秒内到 '$want'（当前 '$s'）"
}

function Audit-Counts($taskKey) {
    # 首次运行时该 task_id 还没有审计行，脚本会退出码 1 并往 stderr 写一行。
    # 那不是错误，只是"还没有"，所以这里兜一个全 0 的基线。
    $json = uv run python scripts/verify_audit_chain.py --task $taskKey --json 2>$null
    if (-not $json) {
        return [pscustomobject]@{
            task_id = $taskKey; total = 0; agents = 0
            lifecycle = 0; human_decision = 0; agent_names = @()
        }
    }
    return ($json | ConvertFrom-Json)
}

$Gateway = $null
$Worker = $null

try {
    Step "0. 清空 demo 状态（让判据可重复）" {
        Psql "DELETE FROM code_review_prs WHERE repo='shing26/moa-gateway'" | Out-Null
        Remove-Item "$Root\data\demo_reviews.json" -ErrorAction SilentlyContinue
        docker exec moa-gateway-redis-1 redis-cli -n 0 FLUSHDB | Out-Null
        # 审计 WAL 是**只追加**的，历史上跑过几轮就会留下几轮的行。所以判据一律
        # 看增量，不看绝对条数——否则脚本只能跑一次。
        $script:AuditBase = (Audit-Counts $TaskKey).total
        Write-Host "   已清库、已清队列"
    }

    Step "1. 起网关（worker 故意**不**起：投递不该依赖 worker 存在）" {
        $script:Gateway = Start-Proc @("-m", "app", "--port", "8081") "gateway.log" "gateway.err"
        $health = Wait-Http "http://127.0.0.1:8081/healthz"
        Write-Host "   healthz: $($health.status) redis=$($health.checks.redis)"
    }

    Step "2. 投递 PR 审查：<1s 返回 202（不跑 agent、不等分析）" {
        $out = uv run python -m app.cli demo review 42
        $out | ForEach-Object { Write-Host "   $_" }
        $text = $out -join "`n"
        Assert-True ($text -match "HTTP 202") "首次投递返回 202"
        # 量的是**网关处理这次投递**的耗时（CLI 自己打的），不是整个 CLI 进程。
        # 后者含 ~1.3s 的解释器启动与 import，拿它当判据只会得到一个与底座无关
        # 的数字。
        if ($text -match "in (\d+)ms") {
            $ms = [int]$Matches[1]
            Assert-True ($ms -lt 1000) "投递耗时 ${ms}ms < 1000ms"
        } else {
            throw "CLI 没输出耗时，无法判定 <1s"
        }
    }

    Step "3. 重复投递：200 idempotent，任务行数不变" {
        $before = (Psql "SELECT count(*) FROM code_review_prs WHERE repo='shing26/moa-gateway'").Trim()
        $out = uv run python -m app.cli demo review 42
        $out | ForEach-Object { Write-Host "   $_" }
        Assert-True ($out -join "`n") -match "HTTP 200" "重投返回 200"
        Assert-True ($out -join "`n") -match "idempotent" "重投被识别为 idempotent"
        $after = (Psql "SELECT count(*) FROM code_review_prs WHERE repo='shing26/moa-gateway'").Trim()
        Assert-True ($before -eq $after) "任务行数 $before -> $after（未新增）"
    }

    Step "4. 起 worker：5 个 agent 真跑（qwen2.5:3b），推进到 waiting_approval" {
        $env:CODE_REVIEW_AUTO_MIGRATE = "0"
        $script:Worker = Start-Proc @("-m", "app.worker") "worker.log" "worker.err"
        Wait-State "waiting_approval" 42 | Out-Null
        Write-Host "   任务状态：waiting_approval"
    }

    Step "5. dry-run 批准：零副作用（不写评论、不记 id、不推进状态）" {
        uv run python -m app.cli demo approve $TaskKey 2>&1 | ForEach-Object { Write-Host "   $_" }
        $st = (Psql "SELECT status || '/' || coalesce(posted_review_id,'-') FROM code_review_prs WHERE pr_number=42").Trim()
        Assert-True ($st -eq "waiting_approval/-") "状态仍是 $st（dry-run 没动它）"
    }

    Step "6. 真批准 -> 写回一条评论" {
        uv run python -m app.cli demo approve $TaskKey --real 2>&1 | ForEach-Object { Write-Host "   $_" }
        Wait-State "done" 42 | Out-Null
        $reviews = (Get-Content "$Root\data\demo_reviews.json" -Raw | ConvertFrom-Json).PSObject.Properties.Count
        Assert-True ($reviews -eq 1) "PR 上有 $reviews 条 review"
    }

    Step "7. 再批准一次：报 already_posted，评论数仍是 1" {
        $out = uv run python -m app.cli demo approve $TaskKey --real 2>&1
        $out | ForEach-Object { Write-Host "   $_" }
        Assert-True ($out -join "`n") -match "already_posted" "重复批准命中幂等"
        $reviews = (Get-Content "$Root\data\demo_reviews.json" -Raw | ConvertFrom-Json).PSObject.Properties.Count
        Assert-True ($reviews -eq 1) "评论数仍是 $reviews（没有重复发）"
    }

    Step "8. 审计链：本轮 9 条且种类齐全" {
        # 计划里写的是"7 条"（5 agent + 1 lifecycle + 1 人工决策）。**实测是 9 条**：
        # lifecycle 不止一条——worker 推 request_approval 一条，审批再推 posting、
        # complete 各一条。计划低估了自己设计的迁移次数，这里按真实值断言，不去凑 7。
        $c = Audit-Counts $TaskKey
        $delta = $c.total - $script:AuditBase
        Write-Host "   本轮新增 $delta 条（总计 $($c.total)）"
        Assert-True ($c.agent_names.Count -eq 5) "五个 agent 名齐全"
        Assert-True ($c.human_decision -ge 1) "有人工决策行（$($c.human_decision) 条）"
        Assert-True ($c.lifecycle -ge 1) "有 lifecycle 行（$($c.lifecycle) 条）"
        Assert-True ($delta -ge 9) "本轮新增 $delta 条（>= 9：5 agent + 3 lifecycle + 1 人工决策）"
        uv run python scripts/verify_audit_chain.py --task $TaskKey
        Assert-True ($LASTEXITCODE -eq 0) "种类校验通过"
    }

    Step "9. kill -9 worker：崩溃遗留的 running 任务由重启后的 worker 接管" {
        # PR 43 专供这一步：上一个任务已经 done，done 是终态，接不住任何东西。
        $out = uv run python -m app.cli demo review 43
        $out | ForEach-Object { Write-Host "   $_" }
        Assert-True ($out -join "`n") -match "HTTP 202" "PR #43 已入队"

        # 等到 running：**认领之后、状态落库之前**是最容易丢任务的窗口，崩在这里
        # 消息留在 PEL、任务行已是 running。必须精确停在这个窗口才有判据意义。
        $deadline = (Get-Date).AddSeconds(90)
        $caught = $false
        while ((Get-Date) -lt $deadline) {
            $s = (Psql "SELECT status FROM code_review_prs WHERE pr_number=43").Trim()
            if ($s -eq "running") { $caught = $true; break }
            Start-Sleep -Milliseconds 200
        }
        Assert-True $caught "抓到了 running 状态"

        # Stop-Process 对已运行进程同样是强杀，与 SIGKILL 同语义：没有 finally、
        # 没有清理钩子，消息只能留在 PEL 里等 XAUTOCLAIM。
        Stop-Process -Id $script:Worker.Id -Force
        $script:Worker.WaitForExit(10000) | Out-Null
        Start-Sleep -Seconds 2
        $stuck = (Psql "SELECT status FROM code_review_prs WHERE pr_number=43").Trim()
        Assert-True ($stuck -eq "running") "worker 已死，任务卡在 $stuck（这正是要验的现象）"

        $script:Worker = Start-Proc @("-m", "app.worker") "worker2.log" "worker2.err"
        Wait-State "waiting_approval" 43 150 | Out-Null
        Write-Host "   重启后 XAUTOCLAIM 接管完成，任务到达 waiting_approval"
    }

    Step "10. 崩溃恢复后的任务审计链同样完整" {
        $key = "shing26/moa-gateway#43@demo0000000000000000000000000000000beef"
        uv run python scripts/verify_audit_chain.py --task $key
        Assert-True ($LASTEXITCODE -eq 0) "恢复后的任务审计种类齐全"
    }
}
finally {
    foreach ($p in @($script:Worker, $script:Gateway)) {
        if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
}

Write-Host ""
if ($Failed.Count -gt 0) {
    Write-Host "FAILED steps: $($Failed -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "golden path 全部通过" -ForegroundColor Green
