# ============================================================
# deepseek-cursor-proxy-tray - Manual launcher for the tray app
# Path: <repo>\scripts\start-tray.ps1
#
# Launches the system-tray supervisor (dscp_tray) using pythonw.exe
# from the project's venv. No console window is created.
#
# The tray app itself:
#   - Starts the upstream deepseek-cursor-proxy subprocess (hidden)
#   - Probes /healthz every 60s with a 10s timeout; on 3 consecutive
#     failures, transitions to STATE_ERROR (red icon + error label)
#     and attempts an automatic restart if the watchdog is enabled
#   - State machine: stopped / starting / running / stopping / error
#   - Right-click menu: Status, Public URL, Copy URL, Restart Proxy,
#     Watchdog toggle, Dismiss Error (error state only), Proxy
#     Settings..., Configure ngrok authtoken..., Reasoning Cache...,
#     Open logs, Quit
# ============================================================

param(
    [switch]$KillExistingTray
)

$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$PythonW    = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $PythonW)) {
    Write-Host "[ERROR] pythonw.exe not found at $PythonW" -ForegroundColor Red
    Write-Host "        Run: uv sync   (from $ProjectDir)" -ForegroundColor Yellow
    exit 1
}

# Detect existing tray instance (looks for any pythonw running -m dscp_tray
# or the legacy tray_app.pyw entry).
$existing = @()
try {
    $existing = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
        Where-Object {
            $_.CommandLine -and (
                ($_.CommandLine -like "*dscp_tray*") -or
                ($_.CommandLine -like "*tray_app.pyw*")
            )
        }
} catch { }

if ($existing) {
    if ($KillExistingTray) {
        foreach ($p in $existing) {
            Write-Host "Stopping existing tray PID $($p.ProcessId)" -ForegroundColor Yellow
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2
    } else {
        Write-Host "Tray app already running (PID $($existing[0].ProcessId))." -ForegroundColor Yellow
        Write-Host "Use -KillExistingTray to replace it." -ForegroundColor Gray
        exit 0
    }
}

Start-Process -FilePath $PythonW `
    -ArgumentList "-m", "dscp_tray" `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Hidden

Start-Sleep -Seconds 2

$now = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -and ($_.CommandLine -like "*dscp_tray*") } |
    Select-Object -First 1

if ($now) {
    Write-Host "Tray app launched (PID $($now.ProcessId))." -ForegroundColor Green
    Write-Host "Look for the icon in your system tray (notification area)."
    Write-Host "Right-click it for actions."
    Write-Host "Logs: $env:USERPROFILE\.deepseek-cursor-proxy\logs\tray.log"
} else {
    Write-Host "Launch likely failed - check tray.log:" -ForegroundColor Red
    Write-Host "  $env:USERPROFILE\.deepseek-cursor-proxy\logs\tray.log"
    exit 1
}
