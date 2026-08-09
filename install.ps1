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
    Write-Host "ERROR: Claude Code (claude) not found in PATH. Install it first: https://claude.com/claude-code" -ForegroundColor Red
    return
}

Write-Host "Installing package (this pulls Whisper + audio dependencies)..."
& python -m pip install --upgrade --quiet "https://github.com/Jogi-OpenSource/claude-code-ptt/archive/main.zip"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: pip install failed (see output above)." -ForegroundColor Red
    return
}

& python -m claude_code_ptt.installer
