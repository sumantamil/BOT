# NIFTY Trading Bot - Background Startup Script
# This keeps the bot running even when you minimize or lock the screen

Write-Host "`n═══════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  🚀 NIFTY Trading Bot - Starting..." -ForegroundColor Green
Write-Host "═══════════════════════════════════════════════════════════" -ForegroundColor Cyan

# Activate virtual environment
& "$PSScriptRoot\.venv\Scripts\Activate.ps1"

# Change to bot directory
Set-Location $PSScriptRoot

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
