# ============================================================
# deepseek-cursor-proxy-tray - Uninstall Auto-Start
# Path: <repo>\scripts\uninstall-autostart.ps1
#
# Removes BOTH the current tray-based task and any legacy tasks
# (logon task + old watchdog). Does NOT stop the currently
# running tray / proxy unless -AlsoStopTray is given.
# ============================================================

param(
    [switch]$AlsoStopTray
)

$ErrorActionPreference = "Stop"

$tasks = @(
    "DeepSeekCursorProxy",            # current tray task & legacy logon task
    "DeepSeekCursorProxy-Watchdog"    # legacy watchdog (replaced by in-app loop)
)

foreach ($name in $tasks) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Write-Host "Removing $name..." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "  Removed." -ForegroundColor Green
    } else {
        Write-Host "$name : not installed, skipping." -ForegroundColor DarkGray
    }
}

if ($AlsoStopTray) {
    $tray = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and (
                ($_.CommandLine -like "*dscp_tray*") -or
                ($_.CommandLine -like "*tray_app.pyw*")
            )
        }
    foreach ($p in $tray) {
        Write-Host "Stopping tray PID $($p.ProcessId)..." -ForegroundColor Yellow
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Auto-start tasks removed." -ForegroundColor Green
if (-not $AlsoStopTray) {
    Write-Host "The currently running tray (if any) was NOT stopped."
    Write-Host "To stop it now:  .\scripts\uninstall-autostart.ps1 -AlsoStopTray"
}
