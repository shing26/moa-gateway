<#
.SYNOPSIS
  一键起停 moa-gateway 的依赖栈：Docker 容器 / Ollama / 网关。

.DESCRIPTION
  演示与本地使用的最小运维脚本。按顺序确保三样东西活着，再等网关 /healthz 通过：

    1) Docker 容器 redis(6380) + postgres(5433)   —— 会话/审计/向量库
    2) Ollama(11434)                              —— LLM 与 embedding
    3) 网关(8081, uvicorn)                        —— python -m app

  背景：2026-09-20 出过一次事故——容器停了 → 网关启动阻塞/退出 → 飞书卡片回调
  打到隧道后 origin 无响应 → 客户端报 200671「回调地址不可达」。演示前先跑本脚本。

.PARAMETER Action
  start（默认）| stop | restart | status

.PARAMETER GatewayPort
  网关端口。省略（0）时依次读 .env 的 GATEWAY_PORT、APP_PORT，都没有则用 8081。
  端口被别的容器/进程占用时脚本会**报出占用者是谁**并拒绝动手——本机 8082 曾被
  另一个项目的容器抢走（2026-10-03），所以这条报错要看，不要直接换端口绕过去。

.PARAMETER Host
  绑定地址，默认 127.0.0.1（只给本机和同机隧道用）。公网暴露走 Tailscale Funnel：
  `tailscale funnel --bg <端口>`。跨机访问才需要 0.0.0.0，且必须先设 DASHBOARD_PASSWORD。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\start_stack.ps1
  powershell -ExecutionPolicy Bypass -File scripts\start_stack.ps1 -Action status

.NOTES
  开机自启（可选，管理员执行一次即可）：
    schtasks /Create /TN "moa-gateway stack" /SC ONLOGON /RL LIMITED ^
      /TR "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File <仓库路径>\scripts\start_stack.ps1"
  取消：
    schtasks /Delete /TN "moa-gateway stack" /F
  说明：stop 只停网关与容器，**不停 Ollama**（它常被本机其他工具共用）。
#>
param(
    [ValidateSet("start", "stop", "status", "restart")]
    [string]$Action = "start",
    # 0 = "去 .env 里找 GATEWAY_PORT / APP_PORT，都没有再用 8081"。显式传 -GatewayPort
    # 仍然优先。此前默认值硬编码 8081，而本机 8082/8083 被别的项目占着（2026-10-03：
    # shoppilot-gateway-1 抢走 8082），于是"改了 .env 的端口却还得每次带参数"，
    # 而漏带时脚本会去和别人的容器抢端口。
    [int]$GatewayPort = 0,
    # 只给本机 / 同机隧道用时保持 127.0.0.1。公网暴露走 Tailscale Funnel
    # （tailscale funnel --bg <端口>），不要为了"能访问"就绑 0.0.0.0：
    # .env 里 DASHBOARD_PASSWORD 与 WEBHOOK_AUTH_TOKEN 默认都是空的。
    [string]$GatewayHost = "127.0.0.1",
    [int]$RedisPort = 6380,
    [int]$PostgresPort = 5433,
    [int]$OllamaPort = 11434
)

$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Resolve-GatewayPort([int]$Requested) {
    if ($Requested -ne 0) { return $Requested }
    $envFile = Join-Path $RepoRoot ".env"
    if (Test-Path $envFile) {
        foreach ($key in @("GATEWAY_PORT", "APP_PORT")) {
            $line = Select-String -Path $envFile -Pattern "^\s*$key\s*=\s*(\d+)\s*$" |
                Select-Object -First 1
            if ($line -and $line.Matches[0].Groups[1].Value) {
                $fromEnv = [int]$line.Matches[0].Groups[1].Value
                if ($fromEnv -ge 1 -and $fromEnv -le 65535) { return $fromEnv }
            }
        }
    }
    return 8081
}

$GatewayPort = Resolve-GatewayPort $GatewayPort

function Test-Port([int]$Port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $ok = $client.ConnectAsync("127.0.0.1", $Port).Wait(1500)
        $client.Close()
        return $ok
    } catch { return $false }
}

function Test-Http([string]$Url) {
    try {
        $res = Invoke-WebRequest -Uri $Url -TimeoutSec 4 -UseBasicParsing
        return ($res.StatusCode -eq 200)
    } catch { return $false }
}

function Get-GatewayPid([int]$Port) {
    $line = (netstat -ano | Select-String ":$Port\s.*LISTENING" | Select-Object -First 1)
    if (-not $line) { return $null }
    return ($line.Line -split '\s+')[-1]
}

function Test-IsOurGateway([int]$Port) {
    # 只认 /healthz 的**字段形状**：对 /health 返回 200 的服务很多（包括占着 8081 的
    # 那个），只有本应用才带 engine/hitl/audit_chain。探索性验收 D1 的教训：
    # 端口被别的服务占住时，"探到 200" 会被误判成"网关已在运行"。
    try {
        $res = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 4 -UseBasicParsing
        return ($res.StatusCode -eq 200 -and $res.Content -match '"engine"')
    } catch { return $false }
}

function Stop-GatewayProcess([int]$Port) {
    # 必须**先确认是本应用**再动手：-Action stop 此前默认端口是 8081，而那正是用户
    # 另一个项目的端口——会把别人的进程 taskkill 掉（2026-09-29 发现）。
    # 顺带修一个潜在 bug：原实现把结果赋给 $pid，那是 PowerShell 的只读自动变量。
    $gwPid = Get-GatewayPid $Port
    if (-not $gwPid) { Write-Host "  [ok] 网关未在运行"; return $true }
    if (-not (Test-IsOurGateway $Port)) {
        Write-Host "  [!!] 端口 $Port 上不是本网关（/healthz 缺本应用字段）——拒绝杀进程，那可能是你的其他服务"
        return $false
    }
    taskkill /F /PID $gwPid | Out-Null
    Write-Host "  [ok] 网关已停止 (pid $gwPid)"
    return $true
}

function Ensure-Containers {
    $running = docker ps --format "{{.Names}}" 2>$null
    $need = @()
    if ($running -notcontains "moa-gateway-redis-1") { $need += "redis" }
    if ($running -notcontains "moa-gateway-postgres-1") { $need += "postgres" }
    if ($need.Count -eq 0) { Write-Host "  [ok] 容器已在运行 (redis/$RedisPort, postgres/$PostgresPort)"; return $true }
    Write-Host "  [..] 拉起容器: $($need -join ', ')"
    Push-Location $RepoRoot
    docker compose -f docker-compose.dev.yml up -d @need | Out-Null
    Pop-Location
    for ($i = 0; $i -lt 30; $i++) {
        if ((Test-Port $RedisPort) -and (Test-Port $PostgresPort)) {
            Write-Host "  [ok] 容器就绪"; return $true
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "  [!!] 容器端口未就绪（redis=$RedisPort pg=$PostgresPort）"
    return $false
}

function Ensure-Ollama {
    if (Test-Http "http://127.0.0.1:$OllamaPort/api/tags") {
        Write-Host "  [ok] Ollama 已在运行 ($OllamaPort)"; return $true
    }
    $candidates = @(
        "D:\Ollama\ollama.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"),
        "ollama"
    )
    $exe = $null
    foreach ($c in $candidates) {
        if ($c -eq "ollama") { if (Get-Command ollama -ErrorAction SilentlyContinue) { $exe = "ollama"; break } }
        elseif (Test-Path $c) { $exe = $c; break }
    }
    if (-not $exe) { Write-Host "  [!!] 找不到 ollama 可执行文件，请手动启动"; return $false }
    Write-Host "  [..] 启动 Ollama"
    Start-Process -FilePath $exe -ArgumentList "serve" -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "ollama.out.log") `
        -RedirectStandardError (Join-Path $LogDir "ollama.err.log")
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-Http "http://127.0.0.1:$OllamaPort/api/tags") {
            Write-Host "  [ok] Ollama 就绪"; return $true
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "  [!!] Ollama 未就绪"
    return $false
}

function Ensure-Gateway {
    if (Test-IsOurGateway $GatewayPort) {
        # "已在运行" ≠ "会应用你刚改的配置"：settings 在进程启动时读一次。
        # 2026-09-29 实测：改了 .env 之后连跑三次 start，/healthz 三次都不变，
        # 用户以为配置没生效。所以这里必须把这句话说出口。
        Write-Host "  [ok] 网关已在运行 ($GatewayPort) —— 改过 .env/代码后要应用新配置请用 -Action restart"
        return $true
    }
    if (Test-Port $GatewayPort) {
        # 说出**是谁**占的：2026-10-03 光说"被别的进程占用"时，排查绕了三步
        # （看 netstat → 看 docker ps → 才发现是 shoppilot-gateway-1 的端口转发）。
        $owner = Get-NetTCPConnection -State Listen -LocalPort $GatewayPort -ErrorAction SilentlyContinue |
            Select-Object -First 1
        $who = "未知进程"
        if ($owner) {
            $procName = (Get-Process -Id $owner.OwningProcess -ErrorAction SilentlyContinue).ProcessName
            $container = (docker ps --format "{{.Names}}`t{{.Ports}}" 2>$null |
                Where-Object { $_ -match ":$GatewayPort->" } | Select-Object -First 1)
            $who = if ($container) {
                "Docker 容器 $($container.Split("`t")[0])（转发到 $GatewayPort，宿主进程 $procName）"
            } else { "进程 $procName (pid $($owner.OwningProcess))" }
        }
        Write-Host "  [!!] 端口 $GatewayPort 被 $who 占用（/healthz 不是本应用的形状）。"
        Write-Host "       改 -GatewayPort / .env 的 GATEWAY_PORT，或自行停掉它——本脚本不会替你杀别人的进程"
        return $false
    }
    $python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { Write-Host "  [!!] 缺少 .venv\Scripts\python.exe（先 uv sync）"; return $false }
    Write-Host "  [..] 启动网关"
    # 端口必须真的传给网关（探索性验收 D1，2026-09-29）：此前 -GatewayPort 只改了健康
    # 探测的目标，`python -m app` 仍按 settings.gateway_port 绑定 —— 端口被别的进程
    # 占住时，网关 bind 失败退出，健康探测却还在探测那个被占的端口，80 秒后才报未就绪。
    Start-Process -FilePath $python -ArgumentList "-m", "app", "--host", $GatewayHost, "--port", "$GatewayPort" -WorkingDirectory $RepoRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "gateway.out.log") `
        -RedirectStandardError (Join-Path $LogDir "gateway.err.log")
    for ($i = 0; $i -lt 40; $i++) {
        if (Test-Http "http://127.0.0.1:$GatewayPort/healthz") {
            Write-Host "  [ok] 网关就绪"; return $true
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "  [!!] 网关未就绪，看 logs\gateway.err.log"
    return $false
}

switch ($Action) {
    "status" {
        Write-Host "moa-gateway 依赖栈状态："
        Write-Host ("  redis   {0}: {1}" -f $RedisPort, $(if (Test-Port $RedisPort) { "UP" } else { "DOWN" }))
        Write-Host ("  postgres {0}: {1}" -f $PostgresPort, $(if (Test-Port $PostgresPort) { "UP" } else { "DOWN" }))
        Write-Host ("  ollama  {0}: {1}" -f $OllamaPort, $(if (Test-Http "http://127.0.0.1:$OllamaPort/api/tags") { "UP" } else { "DOWN" }))
        Write-Host ("  gateway {0}: {1}" -f $GatewayPort, $(if (Test-Http "http://127.0.0.1:$GatewayPort/health") { "UP" } else { "DOWN" }))
    }
    "stop" {
        if (-not (Stop-GatewayProcess $GatewayPort)) { exit 1 }
        Push-Location $RepoRoot
        docker compose -f docker-compose.dev.yml stop redis postgres | Out-Null
        Pop-Location
        Write-Host "  [ok] 容器已停止（Ollama 保持运行，常被其他工具共用）"
    }
    "restart" {
        if (-not (Stop-GatewayProcess $GatewayPort)) { exit 1 }
        Write-Host "  [..] 重新拉起依赖与网关"
        $ok = $true
        $ok = (Ensure-Containers) -and $ok
        $ok = (Ensure-Ollama) -and $ok
        $ok = (Ensure-Gateway) -and $ok
        if ($ok) { Write-Host "重启完成：http://127.0.0.1:$GatewayPort/healthz" }
        else { Write-Host "有组件未就绪，见上面标记 [!!] 的行"; exit 1 }
    }
    "start" {
        Write-Host "启动 moa-gateway 依赖栈："
        $ok = $true
        $ok = (Ensure-Containers) -and $ok
        $ok = (Ensure-Ollama) -and $ok
        $ok = (Ensure-Gateway) -and $ok
        if ($ok) { Write-Host "全部就绪：http://127.0.0.1:$GatewayPort/healthz" }
        else { Write-Host "有组件未就绪，见上面标记 [!!] 的行"; exit 1 }
    }
}
