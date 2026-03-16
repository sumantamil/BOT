# Enable Mobile Access for NIFTY Trading Bot
# This script adds a Windows Firewall rule to allow port 8000
# 
# RIGHT-CLICK THIS FILE → "Run with PowerShell" (as Administrator)

Write-Host "`n════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  🔥 NIFTY Trading Bot - Enable Mobile Access" -ForegroundColor Green
Write-Host "════════════════════════════════════════════════════════`n" -ForegroundColor Cyan

# Check if running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "❌ ERROR: This script must be run as Administrator!" -ForegroundColor Red
    Write-Host "`nPlease:" -ForegroundColor Yellow
    Write-Host "  1. Right-click 'enable_mobile_access.ps1'" -ForegroundColor White
    Write-Host "  2. Select 'Run with PowerShell' AS ADMINISTRATOR" -ForegroundColor White
    Write-Host "`nPress any key to exit..." -ForegroundColor Gray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}

Write-Host "✅ Running as Administrator`n" -ForegroundColor Green

# Add firewall rule
Write-Host "Adding Windows Firewall rule for port 8000..." -ForegroundColor Yellow

try {
    # Check if rule already exists
    $existingRule = Get-NetFirewallRule -DisplayName "NIFTY Trading Bot" -ErrorAction SilentlyContinue
    
    if ($existingRule) {
        Write-Host "⚠️  Firewall rule already exists. Removing old rule..." -ForegroundColor Yellow
        Remove-NetFirewallRule -DisplayName "NIFTY Trading Bot"
    }
    
    # Create new firewall rule
    New-NetFirewallRule -DisplayName "NIFTY Trading Bot" `
                        -Direction Inbound `
                        -LocalPort 8000 `
                        -Protocol TCP `
                        -Action Allow `
                        -ErrorAction Stop | Out-Null
    
    Write-Host "✅ Firewall rule added successfully!`n" -ForegroundColor Green
    
    # Get PC's IP address
    $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
        $_.InterfaceAlias -notlike '*Loopback*' -and 
        $_.IPAddress -notlike '169.254.*'
    } | Select-Object -First 1).IPAddress
    
    if ($ip) {
        Write-Host "════════════════════════════════════════════════════════" -ForegroundColor Cyan
        Write-Host "  📱 MOBILE ACCESS ENABLED!" -ForegroundColor Green
        Write-Host "════════════════════════════════════════════════════════`n" -ForegroundColor Cyan
        
        Write-Host "Your PC's IP Address: " -NoNewline -ForegroundColor White
        Write-Host "$ip" -ForegroundColor Green
        
        Write-Host "`nAccess from mobile browser:" -ForegroundColor Cyan
        Write-Host "  http://${ip}:8000" -ForegroundColor Green -BackgroundColor DarkGreen
        
        Write-Host "`n⚠️  IMPORTANT:" -ForegroundColor Yellow
        Write-Host "  • Phone/tablet must be on SAME Wi-Fi network" -ForegroundColor White
        Write-Host "  • Bot must be running (run start_bot_background.ps1)" -ForegroundColor White
        Write-Host "  • Bookmark the URL on your mobile for quick access!" -ForegroundColor White
    }
    
} catch {
    Write-Host "❌ ERROR: Failed to add firewall rule" -ForegroundColor Red
    Write-Host "   $($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host "`n════════════════════════════════════════════════════════`n" -ForegroundColor Cyan
Write-Host "Press any key to exit..." -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
