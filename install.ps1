# claude-code-ptt one-line installer for Windows.
#
#   irm https://raw.githubusercontent.com/Jogi-OpenSource/claude-code-ptt/main/install.ps1 | iex
#
# Requires Claude Code; installs Python and the Visual C++ runtime if missing.
$ErrorActionPreference = "Stop"

$PythonVersion = "3.12.10"

function Sync-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                (Join-Path $env:USERPROFILE ".local\bin")
}

function Get-PythonVersion {
    # Windows ships a python.exe stub that opens the Store instead of running
    # anything, and it answers Get-Command - so ask the interpreter itself.
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { return $null }
    $reported = & python -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $reported) { return $null }
    return [version]$reported
}

function Get-File {
    param([string]$Url, [string]$Path)
    # curl.exe ships with Windows and draws a readable bar; Invoke-WebRequest
    # reports "Writing request stream", which reads like a failure.
    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        & curl.exe -L --fail --progress-bar -o $Path $Url
        if ($LASTEXITCODE -ne 0) { throw "download failed (curl $LASTEXITCODE)" }
    } else {
        Invoke-WebRequest $Url -OutFile $Path
    }
}

Write-Host "claude-code-ptt installer" -ForegroundColor Cyan

Sync-Path
$version = Get-PythonVersion
if (-not $version -or $version -lt [version]"3.10") {
    Write-Host "Python 3.10+ is missing - installing $PythonVersion from python.org." -ForegroundColor Yellow
    $pyUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
    $pyExe = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
    try {
        Get-File -Url $pyUrl -Path $pyExe
        Write-Host "Installing Python - its own progress window is on screen." -ForegroundColor Yellow
        # tcl/tk stays: the session overlay is tkinter. The test suite and the
        # HTML docs are ~2100 files nothing ever imports.
        $py = Start-Process $pyExe -Wait -PassThru -ArgumentList "/passive",
            "InstallAllUsers=0", "PrependPath=1", "Include_launcher=0",
            "Include_test=0", "Include_doc=0"
        if ($py.ExitCode -ne 0) { throw "installer exited with $($py.ExitCode)" }
    } catch {
        Write-Host "ERROR: could not install Python ($_)." -ForegroundColor Red
        Write-Host "Install it manually from https://python.org (tick 'Add to PATH'), then rerun." -ForegroundColor Red
        return
    }
    Sync-Path
    $version = Get-PythonVersion
    if (-not $version) {
        Write-Host "ERROR: Python installed but is still not on PATH. Open a new PowerShell and rerun." -ForegroundColor Red
        return
    }
}
Write-Host "Python $version" -ForegroundColor Green
# Claude Code is not installed for you: anyone wanting push-to-talk for it
# already has it. Sync-Path has added its .local\bin, so this only reports.
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Claude Code (claude) not found in PATH. Install it first: https://claude.com/claude-code" -ForegroundColor Red
    return
}

# Whisper runs on ctranslate2.dll, which links against the Microsoft Visual
# C++ runtime. A fresh Windows does not ship it, and its absence surfaces much
# later as "Could not find module ctranslate2.dll", which points nowhere.
if (-not (Test-Path (Join-Path $env:SystemRoot "System32\msvcp140.dll"))) {
    Write-Host "The Microsoft Visual C++ runtime is missing; Whisper needs it." -ForegroundColor Yellow
    Write-Host "Installing it now - Windows will ask you for permission." -ForegroundColor Yellow
    $redistUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    $redistExe = Join-Path $env:TEMP "vc_redist.x64.exe"
    try {
        Get-File -Url $redistUrl -Path $redistExe
        $redist = Start-Process $redistExe -Wait -PassThru -ArgumentList "/install","/passive","/norestart"
        # 1638 = a newer runtime is already present, 3010 = installed, wants a reboot
        if ($redist.ExitCode -notin 0, 1638, 3010) {
            throw "installer exited with $($redist.ExitCode)"
        }
    } catch {
        Write-Host "ERROR: could not install the Visual C++ runtime ($_)." -ForegroundColor Red
        Write-Host "Install it manually from $redistUrl, then rerun this installer." -ForegroundColor Red
        return
    }
    Write-Host "Visual C++ runtime installed." -ForegroundColor Green
}

Write-Host "Installing package. This downloads Whisper and its audio dependencies," -ForegroundColor Yellow
Write-Host "several hundred MB, so expect a few minutes. Do not close this window." -ForegroundColor Yellow
& python -m pip install --upgrade "https://github.com/Jogi-OpenSource/claude-code-ptt/archive/main.zip"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed (see output above)." -ForegroundColor Red
    return
}

& python -m claude_code_ptt.installer
