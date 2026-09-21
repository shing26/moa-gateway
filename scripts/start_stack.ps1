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
  start（默认）| stop | status

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
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "start",
    [int]$GatewayPort = 8081,
    [int]$RedisPort = 6380,
    [int]$PostgresPort = 5433,
    [int]$OllamaPort = 11434
)

$ErrorActionPreference = "Continue"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

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

function Get-GatewayPid {
    $line = (netstat -ano | Select-String ":$GatewayPort\s.*LISTENING" | Select-Object -First 1)
    if (-not $line) { return $null }
    return ($line.Line -split '\s+')[-1]
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
    if (Test-Http "http://127.0.0.1:$GatewayPort/health") {
        Write-Host "  [ok] 网关已在运行 ($GatewayPort)"; return $true
    }
    $python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { Write-Host "  [!!] 缺少 .venv\Scripts\python.exe（先 uv sync）"; return $false }
    Write-Host "  [..] 启动网关"
    Start-Process -FilePath $python -ArgumentList "-m", "app" -WorkingDirectory $RepoRoot -WindowStyle Hidden `
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
        $pid = Get-GatewayPid
        if ($pid) { taskkill /F /PID $pid | Out-Null; Write-Host "  [ok] 网关已停止 (pid $pid)" }
        else { Write-Host "  [ok] 网关未在运行" }
        Push-Location $RepoRoot
        docker compose -f docker-compose.dev.yml stop redis postgres | Out-Null
        Pop-Location
        Write-Host "  [ok] 容器已停止（Ollama 保持运行，常被其他工具共用）"
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
