# NIFTY Bot Watchdog — runs until 15:35 IST
# Launch with: Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -File watchdog.ps1" -WorkingDirectory "D:\Users\sundlnu\VS Code BOT\BOT" -WindowStyle Minimized

$BOT_DIR = "D:\Users\sundlnu\VS Code BOT\BOT"
$BOT_PY  = "$BOT_DIR\.venv\Scripts\python.exe"
$LOG     = "$BOT_DIR\watchdog.log"
$restartCount = 0
$checkN  = 0

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $LOG -Value $line
}

function Restart-Bot {
    Log "ACTION: Killing all Python processes..."
    Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Start-Process -FilePath $BOT_PY -ArgumentList "main.py" -WorkingDirectory $BOT_DIR -WindowStyle Hidden
    Start-Sleep -Seconds 12
    $script:restartCount++
    Log "ACTION: Bot restarted (restart #$($script:restartCount))"
}

function Get-BotStatus {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/status" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        return ($r.Content | ConvertFrom-Json)
    } catch { return $null }
}

Log "=== WATCHDOG STARTED — monitoring until 15:35 IST ==="

while ((Get-Date) -lt (Get-Date).Date.AddHours(15).AddMinutes(35)) {
    $checkN++
    $ts = Get-Date -Format 'HH:mm:ss'

    # 1. Port check
    $up = [bool](netstat -ano 2>$null | Select-String ":8000\s.*LISTENING")
    if (-not $up) {
        Log "[$ts] ALERT: Port 8000 not listening — restarting bot"
        Restart-Bot
        continue
    }

    # 2. API health check
    $s = Get-BotStatus
    if (-not $s) {
        Log "[$ts] ALERT: API unresponsive — restarting bot"
        Restart-Bot
        continue
    }

    # 3. Stale analysis check (>5 min = stuck)
    if ($s.last_analysis) {
        $age = [math]::Round(((Get-Date) - [datetime]$s.last_analysis).TotalMinutes, 1)
        if ($age -gt 5) {
            Log "[$ts] ALERT: Analysis stale (${age}min) — restarting bot"
            Restart-Bot
            continue
        }
    }

    # 4. Heartbeat log
    $pnl = "PnL=Rs $($s.daily_pnl)"
    $pos = "Pos=$($s.open_positions)"
    $bal = "Bal=Rs $($s.available_balance)"
    Log "[$ts] OK | $($s.broker) | $pos | $pnl | $bal | Restarts=$restartCount"

    Start-Sleep -Seconds 60
}

Log "=== MARKET CLOSED. Watchdog exiting at $(Get-Date -Format 'HH:mm:ss') ==="
