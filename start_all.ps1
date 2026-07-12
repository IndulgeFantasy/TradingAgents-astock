# TradingAgents-Astock 一键启停脚本
# 启动: Chrome(CDP :9222) + playwright_service(worktrade2 :8765) + tradingagents-web(worktrade)
# 关闭: 按 Ctrl+C 或关闭本窗口，会自动 kill 所有子进程（含进程树）

$ProjectRoot   = "E:\PycharmProject\TradingAgents-astock"
$ChromeExe     = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$ChromeProfile = "E:\ChromeAutomationProfile"
$CondaHook     = "E:\Anaconda\shell\condabin\conda-hook.ps1"
$MainEnv       = "worktrade"
$PwEnv         = "worktrade2"
$LogDir        = Join-Path $ProjectRoot "logs"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

# 收集所有由本脚本拉起的进程 PID（用于退出时 kill 进程树）
$script:LaunchedPids = @()

function Test-Port($port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $port); $c.Close(); return $true
    } catch { return $false }
}

function Wait-Port($port, $timeoutSec = 20) {
    for ($i = 0; $i -lt $timeoutSec; $i++) {
        if (Test-Port $port) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Stop-Tree($processId) {
    if (-not $processId) { return }
    try {
        # /T = 连同所有子进程一起结束，/F = 强制
        & taskkill.exe /PID $processId /T /F 2>$null | Out-Null
    } catch {}
}

function Cleanup {
    Write-Host ""
    Write-Host "==> 正在停止所有服务..." -ForegroundColor Cyan
    foreach ($pid_ in $script:LaunchedPids) {
        Write-Host "    kill PID=$pid_ (含子进程)" -ForegroundColor Gray
        Stop-Tree $pid_
    }
    # 兜底：杀掉可能残留的 streamlit / server.py
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and (
                $_.CommandLine -match "playwright_service[\\/]server\.py" -or
                $_.CommandLine -match "streamlit.*run.*web[\\/]app\.py" -or
                $_.CommandLine -match "tradingagents-web"
            )
        } | ForEach-Object {
            Write-Host "    兜底 kill PID=$($_.ProcessId) [$($_.Name)]" -ForegroundColor Gray
            Stop-Tree $_.ProcessId
        }
    Write-Host "已全部停止。" -ForegroundColor Green
}

# 注册退出 hook：窗口被关闭 / Ctrl+C 触发 finally / PowerShell 退出
Register-EngineEvent PowerShell.Exiting -Action { Cleanup } | Out-Null
[Console]::TreatControlCAsInput = $false

try {
    # --- [1/3] Chrome CDP ---
    Write-Host "==> [1/3] 启动 Chrome (CDP :9222)" -ForegroundColor Cyan
    if (-not (Test-Path $ChromeExe))     { throw "找不到 Chrome: $ChromeExe" }
    if (-not (Test-Path $ChromeProfile)) { New-Item -ItemType Directory -Path $ChromeProfile -Force | Out-Null }

    if (Test-Port 9222) {
        Write-Host "    :9222 已在运行，复用（不受本脚本关闭时影响）" -ForegroundColor Yellow
    } else {
        foreach ($lock in @(
            "$ChromeProfile\SingletonLock",
            "$ChromeProfile\SingletonCookie",
            "$ChromeProfile\SingletonSocket",
            "$ChromeProfile\Default\LOCK"
        )) {
            if (Test-Path $lock) { Remove-Item $lock -Force -ErrorAction SilentlyContinue }
        }

        $p = Start-Process -FilePath $ChromeExe -ArgumentList @(
            "--remote-debugging-port=9222",
            "--user-data-dir=$ChromeProfile",
            "--no-first-run",
            "--no-default-browser-check"
        ) -PassThru
        $script:LaunchedPids += $p.Id
        Write-Host "    Chrome PID=$($p.Id)，等待 CDP 端口..." -ForegroundColor Gray

        if (-not (Wait-Port 9222 15)) {
            throw ":9222 等待超时（可能 profile 被占用，请关闭其它 chrome.exe 后重试）"
        }
        Write-Host "    :9222 已就绪" -ForegroundColor Green
    }

    # --- [2/3] playwright_service (worktrade2) ---
    Write-Host "==> [2/3] 启动 playwright_service (conda: $PwEnv, port :8765)" -ForegroundColor Cyan
    if (-not (Test-Path $CondaHook)) { throw "找不到 conda hook: $CondaHook" }

    if (Test-Port 8765) {
        Write-Host "    :8765 已被占用，复用（不受本脚本关闭时影响）" -ForegroundColor Yellow
    } else {
        $pwLog = Join-Path $LogDir "playwright_service.log"
        $p = Start-Process -FilePath "pwsh" -ArgumentList @(
            "-NoExit",
            "-Command",
            "& '$CondaHook'; conda activate $PwEnv; Set-Location '$ProjectRoot'; python playwright_service/server.py --port 8765 2>&1 | Tee-Object -FilePath '$pwLog'"
        ) -PassThru
        $script:LaunchedPids += $p.Id
        Write-Host "    playwright_service PID=$($p.Id) (log: $pwLog)" -ForegroundColor Gray

        if (Wait-Port 8765 25) {
            Write-Host "    :8765 已就绪" -ForegroundColor Green
        } else {
            Write-Host "    :8765 等待超时，请查看窗口输出或 log" -ForegroundColor Yellow
        }
    }

    # --- [3/3] tradingagents-web (worktrade) ---
    Write-Host "==> [3/3] 启动 tradingagents-web (conda: $MainEnv)" -ForegroundColor Cyan
    $webLog = Join-Path $LogDir "tradingagents_web.log"
    $p = Start-Process -FilePath "pwsh" -ArgumentList @(
        "-NoExit",
        "-Command",
        "& '$CondaHook'; conda activate $MainEnv; Set-Location '$ProjectRoot'; tradingagents-web 2>&1 | Tee-Object -FilePath '$webLog'"
    ) -PassThru
    $script:LaunchedPids += $p.Id
    Write-Host "    tradingagents-web PID=$($p.Id) (log: $webLog)" -ForegroundColor Gray

    Write-Host ""
    Write-Host "全部服务已启动:" -ForegroundColor Green
    Write-Host "  Chrome CDP        : http://127.0.0.1:9222"
    Write-Host "  Playwright Service: http://127.0.0.1:8765/api/health"
    Write-Host "  Web UI            : http://localhost:8501"
    Write-Host ""
    Write-Host "按 Ctrl+C 或关闭本窗口 -> 自动停止全部子进程" -ForegroundColor Yellow
    Write-Host "----------------------------------------------------"

    # 主循环：保持本脚本存活，直到 Ctrl+C
    while ($true) { Start-Sleep -Seconds 3600 }
}
finally {
    Cleanup
}
