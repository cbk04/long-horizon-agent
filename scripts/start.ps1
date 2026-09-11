# Long-Horizon Agent 一键启动脚本
# 用法:  powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# 行为:  检查依赖服务 -> 同步依赖 -> 数据库迁移 -> 开两个窗口分别跑 API 和 Worker

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# ── 读取 .env ──
if (-not (Test-Path "$Root\.env")) {
    Write-Host "[x] 未找到 .env，请先复制: Copy-Item .env.example .env 并填写 LLM 配置" -ForegroundColor Red
    exit 1
}
$envMap = @{}
Get-Content "$Root\.env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        $envMap[$matches[1]] = $matches[2].Trim('"', "'")
    }
}
$MySqlHost   = if ($envMap["MYSQL_HOST"])   { $envMap["MYSQL_HOST"] }   else { "localhost" }
$MySqlPort   = if ($envMap["MYSQL_PORT"])   { [int]$envMap["MYSQL_PORT"] } else { 3306 }
$RedisHost   = if ($envMap["REDIS_HOST"])   { $envMap["REDIS_HOST"] }   else { "localhost" }
$RedisPort   = if ($envMap["REDIS_PORT"])   { [int]$envMap["REDIS_PORT"] } else { 6379 }
$ApiPort     = if ($envMap["API_PORT"])     { [int]$envMap["API_PORT"] } else { 8000 }

# ── 检查 MySQL / Redis 端口 ──
function Test-Port([string]$h, [int]$p, [string]$name) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($h, $p)
        if (-not $task.Wait(2000) -or -not $client.Connected) { throw "timeout" }
        $client.Close()
        Write-Host "[ok] $name 可达 ($h`:$p)" -ForegroundColor Green
        return $true
    } catch {
        Write-Host "[x] $name 连不上 ($h`:$p)，请先启动它" -ForegroundColor Red
        return $false
    }
}
$ok = $true
$ok = (Test-Port $MySqlHost $MySqlPort "MySQL") -and $ok
$ok = (Test-Port $RedisHost $RedisPort "Redis") -and $ok
if (-not $ok) { exit 1 }

# ── 同步依赖 + 数据库迁移 ──
Write-Host "[..] uv sync ..." -ForegroundColor Cyan
uv sync
if (-not $?) { Write-Host "[x] uv sync 失败" -ForegroundColor Red; exit 1 }

Write-Host "[..] alembic upgrade head ..." -ForegroundColor Cyan
uv run alembic upgrade head
if (-not $?) { Write-Host "[x] 数据库迁移失败" -ForegroundColor Red; exit 1 }

# ── 启动两个进程窗口 ──
Write-Host "[..] 启动 API (端口 $ApiPort) 和 Worker ..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$Host.UI.RawUI.WindowTitle = 'LH-Agent API'; uv run uvicorn app.main:app --host 0.0.0.0 --port $ApiPort"
) -WorkingDirectory $Root

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$Host.UI.RawUI.WindowTitle = 'LH-Agent Worker'; uv run python -m app.harness.worker"
) -WorkingDirectory $Root

Start-Sleep -Seconds 3
Write-Host ""
Write-Host "[ok] 已启动:" -ForegroundColor Green
Write-Host "     前端/控制面  http://localhost:$ApiPort"
Write-Host "     健康检查     http://localhost:$ApiPort/health"
Write-Host ""
Write-Host "提示: 任务在 PAUSED(待审批)期间不要关掉 Worker 窗口(图状态在内存里)。"
Write-Host "     停止: 直接关闭两个窗口，或在窗口内 Ctrl+C。"
