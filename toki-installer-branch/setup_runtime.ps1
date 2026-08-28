<#
setup_runtime.ps1
------------------------------------------------------------------------------
Run by installer.iss AFTER files are copied, BEFORE the wizard finishes.
Builds a fully isolated Python 3.12 runtime inside the install folder --
never touches any Python already on this machine, never writes to PATH,
never shows up in "Apps installed" separately from TOKI itself.

WHY EMBEDDED PYTHON INSTEAD OF A REAL SYSTEM INSTALL
------------------------------------------------------------------------------
TOKI needs an EXACT Python 3.12 (kuzu's Windows wheels, PyQt6, and
faster-whisper/openwakeword's prebuilt binaries are all built against
specific CPython ABI tags -- 3.12 is the one this project has been
developed and tested against). Requiring the user to have "the right"
Python already installed -- or silently installing a second, separate
system-wide Python 3.12 alongside whatever they already have on PATH --
is exactly the kind of thing that breaks OTHER tools on their machine or
gets shadowed the next time they update Python for something else. An
embedded copy is invisible to everything but TOKI: it lives entirely at
<install-dir>\runtime\python312, is never registered anywhere, and is
removed cleanly by the uninstaller.

USAGE (called by installer.iss)
------------------------------------------------------------------------------
    powershell -ExecutionPolicy Bypass -File setup_runtime.ps1 `
        -InstallDir "C:\Program Files\TOKI" `
        -IncludeVoice $true

PARAMETERS
------------------------------------------------------------------------------
    -InstallDir     TOKI's install folder (Inno's {app})
    -IncludeVoice   $true  -> also installs openwakeword/faster-whisper/
                              sounddevice (larger download, needed for the
                              Ctrl+K voice pipeline)
                    $false -> skips them; TOKI still runs, just without
                              voice input (matches requirements.txt's own
                              "runs fine without these" note)
#>

param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [bool]$IncludeVoice = $true
)

$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1 (which is what installer.iss explicitly invokes --
# WindowsPowerShell\v1.0\powershell.exe, not PowerShell 7) often defaults
# its .NET SecurityProtocol to TLS 1.0/1.1 depending on the machine's
# Windows/.NET Framework version and configuration. python.org requires
# TLS 1.2+, so without forcing this, the very first Invoke-WebRequest call
# below throws an SSL/TLS handshake exception -- before a single byte of
# Python gets downloaded, let alone extracted. Force it explicitly so this
# doesn't depend on whatever the machine happened to default to.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$PYTHON_VERSION = "3.12.7"   # pin an exact patch version -- never a moving target
$RUNTIME_DIR    = Join-Path $InstallDir "runtime\python312"
$EMBED_URL      = "https://www.python.org/ftp/python/$PYTHON_VERSION/python-$PYTHON_VERSION-embed-amd64.zip"
$GETPIP_URL     = "https://bootstrap.pypa.io/get-pip.py"
$LOG            = Join-Path $InstallDir "install_log.txt"

function Log($msg) {
    $line = "[$(Get-Date -Format 'HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $LOG -Value $line
}

# $ErrorActionPreference = "Stop" only makes PowerShell *cmdlets* throw on
# failure -- it does nothing for external processes like pip.exe or
# python.exe. Those just set $LASTEXITCODE and let the script carry on,
# which meant a real pip failure got logged and then silently ignored,
# eventually reaching "Runtime setup complete." even though it wasn't.
# Call this right after any `& $exe ...` invocation to actually stop on
# failure, with a clear log line saying which step failed.
function Assert-Success($stepName) {
    if ($LASTEXITCODE -ne 0) {
        Log "ERROR: $stepName failed (exit code $LASTEXITCODE). Aborting."
        exit 1
    }
}

# ── 1. Download + extract the embeddable distribution ───────────────────────
Log "Setting up isolated Python $PYTHON_VERSION runtime..."
New-Item -ItemType Directory -Force -Path $RUNTIME_DIR | Out-Null
$zipPath = Join-Path $env:TEMP "toki_python_embed.zip"

if (-not (Test-Path (Join-Path $RUNTIME_DIR "python.exe"))) {
    Log "Downloading embeddable Python from python.org..."
    Invoke-WebRequest -Uri $EMBED_URL -OutFile $zipPath -UseBasicParsing
    Expand-Archive -Path $zipPath -DestinationPath $RUNTIME_DIR -Force
    Remove-Item $zipPath -Force
} else {
    Log "Runtime already present, skipping download (repair/re-run install)."
}

# ── 2. Enable site-packages ──────────────────────────────────────────────────
# The embeddable distribution ships with site-packages DISABLED by default
# (via a "#import site" line in its ._pth file) -- without this edit, pip
# installs into it would be invisible to the interpreter at runtime.
$pthFile = Get-ChildItem -Path $RUNTIME_DIR -Filter "python*._pth" | Select-Object -First 1
if ($pthFile) {
    $content = Get-Content $pthFile.FullName
    $content = $content -replace '^#\s*import site', 'import site'
    if ($content -notcontains "Lib\site-packages") {
        $content += "Lib\site-packages"
    }
    Set-Content -Path $pthFile.FullName -Value $content
    Log "Enabled site-packages in $($pthFile.Name)."
} else {
    Log "WARNING: no ._pth file found -- embeddable layout may have changed upstream."
}

# ── 3. Bootstrap pip (the embeddable package ships with no pip at all) ──────
$pythonExe = Join-Path $RUNTIME_DIR "python.exe"
$getPipPath = Join-Path $env:TEMP "get-pip.py"
if (-not (Test-Path (Join-Path $RUNTIME_DIR "Scripts\pip.exe"))) {
    Log "Bootstrapping pip..."
    Invoke-WebRequest -Uri $GETPIP_URL -OutFile $getPipPath -UseBasicParsing
    & $pythonExe $getPipPath --no-warn-script-location 2>&1 | ForEach-Object { Log $_ }
    Assert-Success "pip bootstrap"
    Remove-Item $getPipPath -Force
}

# ── 4. Install TOKI's dependencies into the isolated runtime ────────────────
$pipExe = Join-Path $RUNTIME_DIR "Scripts\pip.exe"
$reqFile = Join-Path $InstallDir "requirements.txt"

Log "Installing core dependencies (PyQt6, requests, Pillow, kuzu, pywinauto, comtypes, winsdk, pynput, yt-dlp)..."
& $pipExe install --no-warn-script-location `
    "PyQt6>=6.6.0" "requests>=2.31.0" "Pillow>=10.0.0" "kuzu>=0.11.0" `
    "pywinauto>=0.6.8" "comtypes>=1.2.0" "winsdk==1.0.0b10" "pynput>=1.7.6" `
    "yt-dlp>=2024.1.0" 2>&1 | ForEach-Object { Log $_ }
Assert-Success "core dependency install"

if ($IncludeVoice) {
    Log "Installing voice pipeline dependencies (this is the slow one: openwakeword, faster-whisper)..."
    & $pipExe install --no-warn-script-location `
        "openwakeword>=0.4.0" "faster-whisper>=1.0.0" "sounddevice>=0.4.6" 2>&1 | ForEach-Object { Log $_ }
    Assert-Success "voice dependency install"
} else {
    Log "Skipping voice pipeline dependencies (unchecked in setup) -- Ctrl+K voice input will be unavailable."
}

Log "Runtime setup complete."
Log "NOTE: video download/conversion (yt-dlp) also needs the ffmpeg BINARY on PATH -- not pip-installable, not fetched by this script. Missing it just means that one feature degrades with a clear in-app message; nothing else is affected."
