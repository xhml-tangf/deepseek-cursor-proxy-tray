; =============================================================
; deepseek-cursor-proxy-tray Inno Setup installer
; Output: installer\out\dscp-tray-setup-0.1.0.exe
;
; Per-user install (no admin required). Bundles a self-contained
; CPython 3.12.10 (with tkinter + tcl/tk patched in) plus both
; wheels pre-installed into its site-packages.
; =============================================================

#define MyAppName       "DeepSeek Cursor Proxy Tray"
#define MyAppVersion    "0.1.0"
#define MyAppPublisher  "tangf"
#define MyAppURL        "https://github.com/xhml-tangf/deepseek-cursor-proxy-tray"
#define MyTaskName      "DeepSeekCursorProxy"

[Setup]
AppId={{F8B7AE4D-9C5E-4E91-9B3E-D2A1C6A4E711}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\dscp-tray
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
LicenseFile=..\LICENSE
OutputDir=out
OutputBaseFilename=dscp-tray-setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\python\pythonw.exe
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Launch tray automatically at user logon (recommended)"; GroupDescription: "Startup:"; Flags: checkedonce
Name: "installngrok"; Description: "Install ngrok via winget if missing (requires internet)"; GroupDescription: "Dependencies:"; Flags: checkedonce
Name: "launchnow"; Description: "Launch tray immediately after install finishes"; GroupDescription: "Startup:"; Flags: checkedonce

[Files]
Source: "payload\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE";   DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m dscp_tray"; WorkingDir: "{app}"; Comment: "Start the system-tray supervisor"; IconFilename: "{app}\python\pythonw.exe"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
; -- 1. Best-effort: install ngrok via winget if not already on PATH --
; In Inno Setup the literal '{' must be doubled to '{{'.
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""if (-not (Get-Command ngrok -ErrorAction SilentlyContinue)) {{ winget install --id Ngrok.Ngrok --accept-source-agreements --accept-package-agreements --silent }"""; \
    StatusMsg: "Checking ngrok (installing if missing)..."; \
    Flags: runhidden; \
    Tasks: installngrok

; -- 2. Register Task Scheduler logon entry via PowerShell's
;       Register-ScheduledTask (works without admin under user context;
;       schtasks /CREATE may fail on machines where local policy
;       requires elevation even for per-user tasks). --
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""$action = New-ScheduledTaskAction -Execute '{app}\python\pythonw.exe' -Argument '-m dscp_tray' -WorkingDirectory '{app}'; $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Hours 0); $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited; Unregister-ScheduledTask -TaskName '{#MyTaskName}' -Confirm:$false -ErrorAction SilentlyContinue; Register-ScheduledTask -TaskName '{#MyTaskName}' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Auto-start deepseek-cursor-proxy-tray at user logon' | Out-Null"""; \
    StatusMsg: "Registering logon autostart..."; \
    Flags: runhidden; \
    Tasks: autostart

; -- 3. Launch tray now so the user sees the icon immediately. --
Filename: "{app}\python\pythonw.exe"; \
    Parameters: "-m dscp_tray"; \
    WorkingDir: "{app}"; \
    Description: "Launch tray now"; \
    Flags: nowait postinstall skipifsilent runhidden; \
    Tasks: launchnow

[UninstallRun]
; -- Stop any running tray instance --
; Curly braces in inline scriptblocks must be doubled '{{' for Inno.
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Get-CimInstance Win32_Process -Filter \""Name='pythonw.exe'\"" -ErrorAction SilentlyContinue | Where-Object {{ $_.CommandLine -and ($_.CommandLine -like '*dscp_tray*') } | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-NetTCPConnection -LocalPort 9000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; Get-Process -Name ngrok -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"""; \
    Flags: runhidden; \
    RunOnceId: "StopTray"

; -- Remove the scheduled task via PowerShell (matches how we created it). --
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Unregister-ScheduledTask -TaskName '{#MyTaskName}' -Confirm:$false -ErrorAction SilentlyContinue"""; \
    Flags: runhidden; \
    RunOnceId: "DeleteTask"

[UninstallDelete]
; Anything pip created at runtime (e.g. tray.log under data dir is OUTSIDE
; the install dir so it's not touched). Just clean our install tree below.
Type: filesandordirs; Name: "{app}\python"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  // Reserved for future progress hooks. Kept as a stub so the structure is
  // obvious if we later want to print final URLs / next-steps messages.
end;
