while ($true) {
    $now = Get-Date -Format "HH:mm:ss"
    $istHour = (Get-Date).Hour
    $istMin  = (Get-Date).Minute

    # Market closes at 15:30 IST
    if ($istHour -gt 15 -or ($istHour -eq 15 -and $istMin -ge 30)) {
        Add-Content "d:\Users\sundlnu\VS Code BOT\BOT\monitor.log" "$now | Market closed. Stopping watchdog."
        break
    }

    # Check if bot is running
    $procs = Get-Process python -ErrorAction SilentlyContinue
    if (-not $procs) {
        Add-Content "d:\Users\sundlnu\VS Code BOT\BOT\monitor.log" "$now | BOT DOWN — restarting..."
        Set-Location "d:\Users\sundlnu\VS Code BOT\BOT"
        $env:PYTHONIOENCODING = "utf-8"
        Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "main.py" -NoNewWindow -RedirectStandardOutput "bot_stdout.log" -RedirectStandardError "bot_stderr.log"
        Start-Sleep -Seconds 10
    }

    # Check log for DH-905 in last 5 min
    $logLines = Get-Content "d:\Users\sundlnu\VS Code BOT\BOT\bot_stdout.log" -ErrorAction SilentlyContinue | Select-Object -Last 200
    $dh905 = $logLines | Select-String "DH-905" | Select-Object -Last 1
    if ($dh905) {
        Add-Content "d:\Users\sundlnu\VS Code BOT\BOT\monitor.log" "$now | DH-905 detected — IP not whitelisted. Orders failing."
    }

    # Extract daily P&L
    $pnlLine = $logLines | Select-String "Daily P&L:" | Select-Object -Last 1
    if ($pnlLine) {
        Add-Content "d:\Users\sundlnu\VS Code BOT\BOT\monitor.log" "$now | $($pnlLine.Line.Trim())"
    }

    # Check for trailing stop fires
    $trail = $logLines | Select-String "TRAILING STOP triggered" | Select-Object -Last 3
    if ($trail) { $trail | ForEach-Object { Add-Content "d:\Users\sundlnu\VS Code BOT\BOT\monitor.log" "$now | TRAIL: $($_.Line.Trim())" } }

    # Check for time-stop
    $tstop = $logLines | Select-String "TIME-STOP triggered" | Select-Object -Last 3
    if ($tstop) { $tstop | ForEach-Object { Add-Content "d:\Users\sundlnu\VS Code BOT\BOT\monitor.log" "$now | TSTOP: $($_.Line.Trim())" } }

    Start-Sleep -Seconds 180
}
