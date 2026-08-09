# claude-code-ptt one-line installer for Windows.
#
#   irm https://raw.githubusercontent.com/Jogi-OpenSource/claude-code-ptt/main/install.ps1 | iex
#
# Requires: Python 3.10+ and Claude Code already installed.
$ErrorActionPreference = "Stop"

Write-Host "claude-code-ptt installer" -ForegroundColor Cyan

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "ERROR: Python not found in PATH. Install Python 3.10+ from https://python.org (check 'Add to PATH'), then rerun." -ForegroundColor Red
    return
}
$version = & python -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))"
if ([version]$version -lt [version]"3.10") {
    Write-Host "ERROR: Python $version found, but 3.10+ is required." -ForegroundColor Red
    return
}
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    # Claude Code installs into %USERPROFILE%\.local\bin, which a shell that was
    # already open when Claude Code was installed does not have on its PATH yet.
    $claudeBin = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path $claudeBin) { $env:Path += ";$claudeBin" }
}
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
        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            & curl.exe -L --fail --progress-bar -o $redistExe $redistUrl
            if ($LASTEXITCODE -ne 0) { throw "download failed (curl $LASTEXITCODE)" }
        } else {
            Invoke-WebRequest $redistUrl -OutFile $redistExe
        }
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
