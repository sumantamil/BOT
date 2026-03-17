$last = (Get-Content "trading_bot.log" -ErrorAction SilentlyContinue).Count
if (-not $last) { $last = 0 }

for ($i = 1; $i -le 30; $i++) {
    Start-Sleep 60
    $ts = (Get-Date).ToString("HH:mm:ss")

    $st  = $null; $pnl = $null
    try { $st  = Invoke-RestMethod "http://127.0.0.1:8000/api/status" -TimeoutSec 5 } catch {}
    try { $pnl = Invoke-RestMethod "http://127.0.0.1:8000/api/pnl"    -TimeoutSec 5 } catch {}

    if ($st) {
        $pos  = $st.open_positions
        $dp   = if ($pnl) { $pnl.daily_pnl }   else { $st.daily_pnl }
        $cp   = if ($pnl) { $pnl.current_pnl }  else { $st.current_pnl }
        $mode = $st.state
        Write-Host "[$ts] $mode | pos=$pos | daily=Rs$dp | live=Rs$cp" -ForegroundColor Cyan
    } else {
        Write-Host "[$ts] BOT DOWN — restarting..." -ForegroundColor Red
        Get-Process python* -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep 3
        Start-Process ".venv\Scripts\python.exe" -ArgumentList "main.py" -WorkingDirectory "D:\Users\sundlnu\VS Code BOT\BOT" -WindowStyle Hidden
        Start-Sleep 10
        continue
    }

    # Print new notable log lines since last check
    $all = Get-Content "trading_bot.log" -ErrorAction SilentlyContinue
    if ($all -and $all.Count -gt $last) {
        $newLines = $all[$last..($all.Count - 1)]
        $last = $all.Count
        $newLines | Select-String "ERROR|order placed|Trade recorded|STOP-LOSS|TRAILING|TARGET reached|auto-clos|DH-905" |
            ForEach-Object {
                $line = $_.Line
                if ($line.Length -gt 130) { $line = $line.Substring($line.Length - 130) }
                if ($_.Line -match "ERROR|DH-") {
                    Write-Host "  [ERR] $line" -ForegroundColor Red
                } else {
                    Write-Host "  [TRD] $line" -ForegroundColor Green
                }
            }
    } else {
        $last = if ($all) { $all.Count } else { 0 }
    }
}
Write-Host "Monitor finished." -ForegroundColor Yellow
