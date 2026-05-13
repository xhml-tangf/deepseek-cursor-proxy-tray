# ============================================================
# Reproducible installer build for deepseek-cursor-proxy-tray.
# Path: installer\build-installer.ps1
#
# Produces: installer\out\dscp-tray-setup-<version>.exe
#
# Pipeline:
#   1. Build the local wheel via uv build         (-> ..\dist\)
#   2. Shallow-clone the upstream proxy + build it  (-> upstream-clone\dist\)
#   3. Download CPython 3.12.10 embeddable + get-pip.py
#   4. Patch the embeddable's _pth to enable site-packages
#   5. Copy tkinter + tcl/tk from the host Python (must be 3.12.x at the
#      same patch level as the embeddable)
#   6. Bootstrap pip into the embeddable
#   7. pip install upstream + transitive deps, then our wheel (--no-deps)
#   8. Compile the .iss with ISCC.exe
#
# Requires (on the build machine, not on end-user machines):
#   - uv on PATH (or in %USERPROFILE%\.local\bin)
#   - git on PATH
#   - Python 3.12.x installed somewhere (we copy tkinter from it)
#   - Inno Setup 6 installed (we accept either the per-user or system path)
# ============================================================

param(
    [string]$PythonHost,     # path to a normal Python 3.12.x install to source tkinter from
    [string]$PythonVersion = "3.12.10",
    [string]$ISCC           # path to ISCC.exe; auto-detected if blank
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir   = Split-Path -Parent $ScriptDir
$Payload   = Join-Path $ScriptDir "payload"
$OutDir    = Join-Path $ScriptDir "out"

# ---- locate tooling ----
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] uv not on PATH" -ForegroundColor Red; exit 1
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] git not on PATH" -ForegroundColor Red; exit 1
}
if (-not $ISCC) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { $ISCC = $c; break } }
}
if (-not $ISCC -or -not (Test-Path $ISCC)) {
    Write-Host "[ERROR] ISCC.exe not found. Install Inno Setup 6 or pass -ISCC <path>." -ForegroundColor Red
    exit 1
}
Write-Host "ISCC: $ISCC"

# ---- locate host Python 3.12.x ----
if (-not $PythonHost) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "C:\Python312\python.exe",
        "C:\Program Files\Python312\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { $PythonHost = $c; break } }
}
if (-not $PythonHost -or -not (Test-Path $PythonHost)) {
    Write-Host "[ERROR] Could not find a host Python 3.12.x. Pass -PythonHost <path\to\python.exe>." -ForegroundColor Red
    exit 1
}
$PythonHostDir = Split-Path -Parent $PythonHost
Write-Host "Host Python: $PythonHost"

# ---- 1. build local wheel ----
Write-Host ""
Write-Host "[1/8] Building local wheel..." -ForegroundColor Cyan
Push-Location $RepoDir
uv build | Out-Null
Pop-Location
$LocalWheel = (Get-ChildItem "$RepoDir\dist\*.whl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
Write-Host "  -> $LocalWheel"

# ---- 2. clone + build upstream wheel ----
Write-Host ""
Write-Host "[2/8] Building upstream wheel..." -ForegroundColor Cyan
$UpstreamClone = Join-Path $ScriptDir "upstream-clone"
if (Test-Path $UpstreamClone) { Remove-Item -Recurse -Force $UpstreamClone }
git clone --depth 1 https://github.com/yxlao/deepseek-cursor-proxy.git $UpstreamClone | Out-Null
Push-Location $UpstreamClone
uv build | Out-Null
Pop-Location
$UpstreamWheel = (Get-ChildItem "$UpstreamClone\dist\*.whl").FullName
Write-Host "  -> $UpstreamWheel"

# ---- 3. download embeddable + get-pip.py ----
Write-Host ""
Write-Host "[3/8] Downloading Python $PythonVersion embeddable..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $Payload | Out-Null
$EmbedZip = Join-Path $ScriptDir "python-embed-$PythonVersion.zip"
if (-not (Test-Path $EmbedZip)) {
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" `
        -OutFile $EmbedZip -UseBasicParsing
}
$PayloadPy = Join-Path $Payload "python"
if (Test-Path $PayloadPy) { Remove-Item -Recurse -Force $PayloadPy }
Expand-Archive $EmbedZip -DestinationPath $PayloadPy -Force

$GetPip = Join-Path $Payload "get-pip.py"
if (-not (Test-Path $GetPip)) {
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPip -UseBasicParsing
}

# ---- 4. patch _pth ----
Write-Host ""
Write-Host "[4/8] Patching python312._pth..." -ForegroundColor Cyan
$pth = Join-Path $PayloadPy "python312._pth"
"python312.zip`n.`nLib`nLib\site-packages`n`nimport site" | Set-Content $pth -Encoding ASCII

# ---- 5. copy tkinter + tcl/tk from host ----
Write-Host ""
Write-Host "[5/8] Bundling tkinter from host Python..." -ForegroundColor Cyan
foreach ($f in @("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll")) {
    $src = Join-Path $PythonHostDir "DLLs\$f"
    if (Test-Path $src) { Copy-Item $src (Join-Path $PayloadPy $f) }
}
$tkLib = Join-Path $PythonHostDir "Lib\tkinter"
$tcl   = Join-Path $PythonHostDir "tcl"
if (-not (Test-Path $tkLib) -or -not (Test-Path $tcl)) {
    Write-Host "[ERROR] Host Python missing tkinter/tcl trees: $tkLib , $tcl" -ForegroundColor Red
    exit 1
}
New-Item -ItemType Directory -Force (Join-Path $PayloadPy "Lib\tkinter") | Out-Null
Copy-Item -Recurse -Force "$tkLib\*" (Join-Path $PayloadPy "Lib\tkinter")
Copy-Item -Recurse -Force $tcl       $PayloadPy

# ---- 6. bootstrap pip ----
Write-Host ""
Write-Host "[6/8] Bootstrapping pip..." -ForegroundColor Cyan
$EmbedExe = Join-Path $PayloadPy "python.exe"
& $EmbedExe $GetPip --no-warn-script-location | Out-Null
New-Item -ItemType Directory -Force (Join-Path $PayloadPy "Scripts") | Out-Null

# ---- 7. pip install wheels ----
Write-Host ""
Write-Host "[7/8] Installing wheels into the embedded Python..." -ForegroundColor Cyan
& $EmbedExe -m pip install --no-warn-script-location `
    $UpstreamWheel pystray Pillow PyYAML six | Out-Null
& $EmbedExe -m pip install --no-warn-script-location --no-deps `
    $LocalWheel | Out-Null

# Smoke test
& $EmbedExe -c "import dscp_tray.tray as t; argv,_ = t._resolve_proxy_command(); assert argv, 'proxy not resolvable'; import tkinter; r=tkinter.Tk(); r.destroy(); print('smoke OK')"

# ---- 8. compile .iss ----
Write-Host ""
Write-Host "[8/8] Compiling Inno Setup script..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force $OutDir | Out-Null
& $ISCC (Join-Path $ScriptDir "dscp-tray.iss")
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] ISCC failed with exit $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Done. Output:" -ForegroundColor Green
Get-ChildItem $OutDir | Select-Object Name, @{N='Size';E={'{0:N1} MB' -f ($_.Length / 1MB)}}
