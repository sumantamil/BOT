# NIFTY Trading Bot - Background Startup Script
# This keeps the bot running even when you minimize or lock the screen

Write-Host "`n═══════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  🚀 NIFTY Trading Bot - Starting..." -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════════════════" -ForegroundColor Cyan

# Activate virtual environment
& "$PSScriptRoot\.venv\Scripts\Activate.ps1"

# Change to bot directory
Set-Location $PSScriptRoot

# ── Auto-start Ollama AI server if installed and AI_ENABLED=true ────────────
$ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
$aiEnabled = $false
if (Test-Path .env) {
    if ((Get-Content .env -Raw) -match 'AI_ENABLED\s*=\s*true') { $aiEnabled = $true }
}
if ($aiEnabled -and (Test-Path $ollamaExe)) {
    $ollamaRunning = $false
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:11434" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $ollamaRunning = $true }
    } catch {}
    if (-not $ollamaRunning) {
        Write-Host "  🤖 Starting Ollama AI server..." -ForegroundColor Cyan
        Start-Process $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
        Start-Sleep -Seconds 3
        Write-Host "  ✅ Ollama AI server running on http://localhost:11434" -ForegroundColor Green
    } else {
        Write-Host "  ✅ Ollama AI server already running" -ForegroundColor Green
    }
} elseif ($aiEnabled -and -not (Test-Path $ollamaExe)) {
    Write-Host "  ⚠️  AI_ENABLED=true but Ollama not found — AI validation will be skipped" -ForegroundColor Yellow
}

# Get PC's IP address for mobile access
$pcIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.InterfaceAlias -notlike '*Loopback*' -and 
    $_.IPAddress -notlike '169.254.*'
} | Select-Object -First 1).IPAddress

# Check Telegram bot status from .env
$telegramEnabled = $false
$telegramConfigured = $false
if (Test-Path .env) {
    $envContent = Get-Content .env -Raw
    if ($envContent -match 'ALERT_TELEGRAM_ENABLED\s*=\s*true') {
        $telegramEnabled = $true
    }
    if ($envContent -match 'ALERT_TELEGRAM_BOT_TOKEN\s*=\s*[^\s]+' -and $envContent -match 'ALERT_TELEGRAM_CHAT_ID\s*=\s*[^\s]+') {
        $telegramConfigured = $true
    }
}

# Display access URLs
Write-Host "`n📱 DASHBOARD ACCESS:" -ForegroundColor Yellow
Write-Host "  • From this PC:       http://localhost:8000" -ForegroundColor White
if ($pcIP) {
    Write-Host "  • From mobile/tablet: http://${pcIP}:8000" -ForegroundColor Green
    Write-Host "    (Phone must be on same Wi-Fi network)" -ForegroundColor Gray
}

Write-Host "`n💬 TELEGRAM ALERTS:" -ForegroundColor Yellow
if ($telegramEnabled -and $telegramConfigured) {
    Write-Host "  ✅ ENABLED - You'll receive trade notifications on Telegram" -ForegroundColor Green
} elseif ($telegramConfigured -and -not $telegramEnabled) {
    Write-Host "  ⚠️  CONFIGURED but DISABLED (set ALERT_TELEGRAM_ENABLED=true in .env)" -ForegroundColor Yellow
} else {
    Write-Host "  ⚠️  NOT CONFIGURED - See README for Telegram setup instructions" -ForegroundColor Gray
}


Write-Host "`n💡 TIPS:" -ForegroundColor Cyan
Write-Host "  • Minimize this window (DON'T close it!)" -ForegroundColor White
Write-Host "  • Lock your screen - bot keeps running ✓" -ForegroundColor White
Write-Host "  • Press Ctrl+C to stop the bot" -ForegroundColor White

Write-Host "`nBot started at: $(Get-Date -Format 'HH:mm:ss')" -ForegroundColor Gray
Write-Host "═══════════════════════════════════════════════════════════`n" -ForegroundColor Cyan

# Run the bot
python main.py

# This runs until you press Ctrl+C or close the window
