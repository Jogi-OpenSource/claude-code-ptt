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

function Publish-EnvironmentChange {
    # Writing the registry is only half the job: explorer.exe keeps its own
    # copy of the environment and hands that copy to every console it starts,
    # so without this broadcast the PATH we just wrote stays invisible in new
    # windows until the next sign-in. .NET's SetEnvironmentVariable sends it
    # for you - but that one writes REG_SZ and would flatten %VARS%.
    if (-not ("Ccptt.Native" -as [type])) {
        Add-Type -Namespace Ccptt -Name Native -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(
    IntPtr hWnd, uint msg, IntPtr wParam, string lParam,
    uint flags, uint timeout, out UIntPtr result);
'@
    }
    # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG, 5s per window: a
    # hung window must not hold the installer hostage.
    $answer = [UIntPtr]::Zero
    [void][Ccptt.Native]::SendMessageTimeout(
        [IntPtr]0xffff, 0x1A, [IntPtr]::Zero, "Environment", 0x2, 5000, [ref]$answer)
}

function Add-ToUserPath {
    # Claude Code drops claude.exe in a directory that is not on the
    # persistent PATH (%USERPROFILE%\.local\bin for the native install), so a
    # NEW console cannot find `claude` - and neither can the MCP adapter we
    # are about to register.
    param([string]$Directory)

    if (-not (Test-Path $Directory)) { return }
    $key = Get-Item "HKCU:\Environment"
    # Read the RAW value: PowerShell would otherwise hand back an expanded
    # string, and writing that back turns %VARS% in the user's PATH into
    # literals for good.
    $raw = $key.GetValue("Path", "", "DoNotExpandEnvironmentNames")
    # Compare expanded, because %USERPROFILE%\.local\bin and the spelled-out
    # path are the same directory and a second copy helps nobody.
    $entries = $raw -split ";" | ForEach-Object {
        [Environment]::ExpandEnvironmentVariables($_).TrimEnd("\")
    }
    if ($entries -contains $Directory.TrimEnd("\")) { return }
    $updated = ($raw.TrimEnd(";") + ";" + $Directory).TrimStart(";")
    Set-ItemProperty -Path "HKCU:\Environment" -Name Path -Value $updated -Type ExpandString
    Write-Host "Added $Directory to your PATH." -ForegroundColor Green
    try {
        Publish-EnvironmentChange
    } catch {
        Write-Host "Note: running programs were not notified ($_); a sign-out picks it up." -ForegroundColor Yellow
    }
}

function Test-ClaudeOnFreshPath {
    # The only honest answer to "will a NEW window find claude?" comes from a
    # PATH rebuilt out of the registry - a child process would otherwise just
    # inherit the PATH Sync-Path fixed up in here.
    $probe = '$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine")' +
             ' + ";" + [Environment]::GetEnvironmentVariable("Path", "User");' +
             ' (Get-Command claude -ErrorAction SilentlyContinue).Source'
    $shell = (Get-Process -Id $PID).Path
    if (-not $shell) { $shell = "powershell.exe" }
    # Base64 rather than -Command: the probe is full of quotes, and native
    # argument passing mangles those.
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probe))
    return (& $shell -NoProfile -EncodedCommand $encoded | Select-Object -First 1)
}

function Get-PythonVersion {
    # Windows ships a python.exe stub that opens the Store instead of running
    # anything, and it answers Get-Command - so ask the interpreter itself.
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { return $null }
    # The stub answers on stderr, and with ErrorActionPreference "Stop" that
    # alone aborts the whole installer - so this one call runs relaxed.
    $reported = $null
    try {
        $ErrorActionPreference = "Continue"
        $reported = & python -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))" 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $reported) { return $null }
    return [version]$reported
}

function Write-Status {
    # One self-overwriting line, padded so a longer previous line leaves no
    # tail behind the carriage return.
    param([string]$Text, [switch]$Final)
    $width = [Math]::Max(40, $Host.UI.RawUI.WindowSize.Width - 1)
    if ($Text.Length -gt $width) { $Text = $Text.Substring(0, $width) }
    if ($Final) { Write-Host ("`r" + $Text.PadRight($width)) }
    else { Write-Host -NoNewline ("`r" + $Text.PadRight($width)) }
}

function Wait-WithProgress {
    # A silent installer is indistinguishable from a hung one. Report what is
    # landing on disk, or what the installer's own log says it is doing.
    param([System.Diagnostics.Process]$Process, [string]$Label,
          [string]$WatchDir, [string]$LogFile)

    $start = Get-Date
    while (-not $Process.HasExited) {
        $line = "  {0} - {1}s" -f $Label, [int]((Get-Date) - $start).TotalSeconds
        if ($WatchDir) {
            # Counted off disk, not via FileSystemWatcher: Windows Installer
            # writes temp files and renames them, so Created events barely fire.
            $written = @(Get-ChildItem $WatchDir -Recurse -File -ErrorAction SilentlyContinue)
            $newest = $written | Sort-Object LastWriteTime | Select-Object -Last 1
            if ($newest) { $line += " | {0} files | {1}" -f $written.Count, $newest.Name }
        } elseif ($LogFile -and (Test-Path $LogFile)) {
            $tail = Get-Content $LogFile -Tail 1 -ErrorAction SilentlyContinue
            if ($tail) { $line += " | " + $tail.Trim() }
        }
        Write-Status $line
        Start-Sleep -Milliseconds 700
    }
    Write-Status ("  {0} - done after {1}s" -f $Label, [int]((Get-Date) - $start).TotalSeconds) -Final
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
        $py = Start-Process $pyExe -PassThru -ArgumentList "/passive",
            "InstallAllUsers=0", "PrependPath=1", "Include_launcher=0",
            "Include_test=0", "Include_doc=0"
        Wait-WithProgress -Process $py -Label "Python installer" `
            -WatchDir (Join-Path $env:LOCALAPPDATA "Programs")
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
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    Write-Host "ERROR: Claude Code (claude) not found in PATH. Install it first: https://claude.com/claude-code" -ForegroundColor Red
    return
}
# Persist the directory claude REALLY sits in: the native installer uses
# .local\bin, an npm install puts it in %APPDATA%\npm, and only the one it
# came from is any use to a new console.
$claudeDir = Join-Path $env:USERPROFILE ".local\bin"
if ($claude.Source) { $claudeDir = Split-Path $claude.Source -Parent }
Add-ToUserPath $claudeDir

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
        # The redistributable draws no progress at all; its own log is the only
        # place that says which package it is on.
        $redistLog = Join-Path $env:TEMP "vc_redist.install.log"
        $redist = Start-Process $redistExe -PassThru -ArgumentList "/install","/passive","/norestart","/log",$redistLog
        Wait-WithProgress -Process $redist -Label "Visual C++ runtime" -LogFile $redistLog
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

# Last word on the PATH: the whole point is that the NEXT window works, and
# that is worth measuring instead of assuming.
try { $fresh = Test-ClaudeOnFreshPath } catch { $fresh = $null }
if ($fresh) {
    Write-Host "A new console will find claude at $fresh." -ForegroundColor Green
} else {
    Write-Host "WARNING: a new console will NOT find claude yet." -ForegroundColor Red
    Write-Host "Sign out of Windows and back in, then run 'claude' to check." -ForegroundColor Red
}
