# ============================================================
# deepseek-cursor-proxy-tray - Install Auto-Start (Task Scheduler)
# Path: <repo>\scripts\install-autostart.ps1
#
# Registers a SINGLE scheduled task that launches the tray app
# at user logon via pythonw.exe (no console window flashes).
#
# The tray app owns the proxy subprocess and runs an in-process
# health-check loop, so no separate watchdog task is needed.
# Any legacy watchdog task from earlier versions is removed.
#
# Run as: regular user (no admin required).
# ============================================================

param()

$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$PythonW    = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $PythonW)) {
    Write-Host "[ERROR] pythonw.exe not found at $PythonW" -ForegroundColor Red
    Write-Host "        Run from $ProjectDir : uv sync" -ForegroundColor Yellow
    exit 1
}

# --- Remove legacy watchdog task (no longer needed) ---
$legacyWatchdog = "DeepSeekCursorProxy-Watchdog"
if (Get-ScheduledTask -TaskName $legacyWatchdog -ErrorAction SilentlyContinue) {
    Write-Host "Removing legacy watchdog task: $legacyWatchdog" -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $legacyWatchdog -Confirm:$false
}

# --- Register/replace tray autostart task ---
$taskName = "DeepSeekCursorProxy"

Write-Host "Registering logon task: $taskName" -ForegroundColor Cyan

$action = New-ScheduledTaskAction `
    -Execute $PythonW `
    -Argument "-m dscp_tray" `
    -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT15S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Auto-start deepseek-cursor-proxy-tray at user logon" | Out-Null

Write-Host "  OK: $taskName -> $PythonW -m dscp_tray" -ForegroundColor Green

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host " Auto-start installed"                            -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host " The tray app will launch silently 15s after each logon."
Write-Host " It owns the proxy subprocess (no console window)."
Write-Host " A built-in health check (60s interval, 3 strikes) restarts"
Write-Host " the proxy on crash or hang. Persistent failures land in"
Write-Host " STATE_ERROR (red icon) with a labeled cause - acknowledge"
Write-Host " via the tray's 'Dismiss Error' menu or click 'Restart Proxy'."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Trigger now:   Start-ScheduledTask -TaskName '$taskName'"
Write-Host "  View task:     Get-ScheduledTask -TaskName '$taskName' | Format-List"
Write-Host "  Tray log:      Get-Content `$env:USERPROFILE\.deepseek-cursor-proxy\logs\tray.log -Tail 30"
Write-Host "  Uninstall:     .\scripts\uninstall-autostart.ps1"
