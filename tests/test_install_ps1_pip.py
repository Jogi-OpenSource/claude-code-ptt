"""Integration tests for pip progress reporting in install.ps1."""
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="install.ps1 is Windows-only"
)

INSTALL_PS1 = Path(__file__).resolve().parent.parent / "install.ps1"
PIP_STATUS_INTERVAL_SECONDS = 0.7

PROGRESS_HARNESS = r"""
param([string]$Script, [string]$Package, [int]$OutputDelayMs)
$ErrorActionPreference = "Stop"

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $Script, [ref]$tokens, [ref]$errors)
foreach ($name in "Write-Status", "Wait-WithProgress") {
    $function = $ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true) | Select-Object -First 1
    if (-not $function) { throw "Function $name not found" }
    Invoke-Expression $function.Extent.Text
}

$packageName = [IO.Path]::GetFileName($Package).Replace("_", "-")
$escapedPackage = $packageName.Replace("'", "''")
$command = "Write-Output 'Processing $escapedPackage'; " +
    "Start-Sleep -Milliseconds $OutputDelayMs; exit 0"
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = "powershell.exe"
$startInfo.Arguments = '-NoProfile -Command "{0}"' -f $command
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$process = [Diagnostics.Process]::Start($startInfo)
$output = @(Wait-WithProgress -Process $process -Label "pip install" -ReadOutput)
$exitCode = $process.ExitCode
$process.Dispose()
Write-Output "EXIT_CODE=$exitCode"
"""

PIP_HARNESS = r"""
param([string]$Script, [string]$Python, [string]$Package)
$ErrorActionPreference = "Stop"

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $Script, [ref]$tokens, [ref]$errors)
foreach ($name in "Write-Status", "Wait-WithProgress", "Invoke-PipInstall") {
    $function = $ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true) | Select-Object -First 1
    if (-not $function) { throw "Function $name not found" }
    Invoke-Expression $function.Extent.Text
}

$exitCode = Invoke-PipInstall -Python $Python -Package $Package
Write-Output "EXIT_CODE=$exitCode"
"""

BURST_HARNESS = r"""
param([string]$Script)
$ErrorActionPreference = "Stop"

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $Script, [ref]$tokens, [ref]$errors)
foreach ($name in "Write-Status", "Wait-WithProgress") {
    $function = $ast.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $name
    }, $true) | Select-Object -First 1
    Invoke-Expression $function.Extent.Text
}

$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = "powershell.exe"
$startInfo.Arguments = '-NoProfile -Command "1..100 | ForEach-Object { Write-Output line-$_ }"'
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$process = [Diagnostics.Process]::Start($startInfo)
$stopwatch = [Diagnostics.Stopwatch]::StartNew()
$output = @(Wait-WithProgress -Process $process -Label "burst" -ReadOutput)
$stopwatch.Stop()
Write-Output "LINES=$($output.Count)"
Write-Output "ELAPSED=$($stopwatch.Elapsed.TotalSeconds)"
"""


def _write_wheel(path):
    dist_info = "progress_demo-0.0.1.dist-info"
    files = {
        "progress_demo/__init__.py": '__version__ = "0.0.1"\n',
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            "Name: progress-demo\n"
            "Version: 0.0.1\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: ccptt-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
    }
    record = "".join(f"{name},,\n" for name in files)
    files[f"{dist_info}/RECORD"] = record + f"{dist_info}/RECORD,,\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


@pytest.fixture(scope="module")
def pip_environment(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pip-progress")
    venv = tmp / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)

    wheel = tmp / "progress_demo-0.0.1-py3-none-any.whl"
    _write_wheel(wheel)
    invalid_wheel = tmp / "broken_demo-0.0.1-py3-none-any.whl"
    invalid_wheel.write_text("not a wheel", encoding="utf-8")

    pip_harness = tmp / "pip-harness.ps1"
    pip_harness.write_text(PIP_HARNESS, encoding="utf-8")
    progress_harness = tmp / "progress-harness.ps1"
    progress_harness.write_text(PROGRESS_HARNESS, encoding="utf-8")
    burst_harness = tmp / "burst-harness.ps1"
    burst_harness.write_text(BURST_HARNESS, encoding="utf-8")
    return {
        "python": venv / "Scripts" / "python.exe",
        "wheel": wheel,
        "invalid_wheel": invalid_wheel,
        "pip_harness": pip_harness,
        "progress_harness": progress_harness,
        "burst_harness": burst_harness,
    }


def _run_pip(pip_environment, package):
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(pip_environment["pip_harness"]),
            "-Script",
            str(INSTALL_PS1),
            "-Python",
            str(pip_environment["python"]),
            "-Package",
            str(package),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _run_progress(pip_environment, package):
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(pip_environment["progress_harness"]),
            "-Script",
            str(INSTALL_PS1),
            "-Package",
            str(package),
            "-OutputDelayMs",
            str(round(PIP_STATUS_INTERVAL_SECONDS * 2 * 1000)),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_wait_with_progress_reports_latest_output_and_success(pip_environment):
    result = _run_progress(pip_environment, pip_environment["wheel"])

    assert "EXIT_CODE=0" in result.stdout
    assert re.search(r"pip install - \d+s \| .*progress-demo", result.stdout)


def test_pip_install_reports_success(pip_environment):
    result = _run_pip(pip_environment, pip_environment["wheel"])

    assert "EXIT_CODE=0" in result.stdout


def test_pip_install_passes_expected_arguments():
    source = INSTALL_PS1.read_text(encoding="utf-8")
    assert "-m pip install --upgrade --progress-bar off" in source


def test_output_burst_is_drained_without_one_sleep_per_line(pip_environment):
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(pip_environment["burst_harness"]),
            "-Script",
            str(INSTALL_PS1),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    lines = re.search(r"LINES=(\d+)", result.stdout)
    elapsed = re.search(r"ELAPSED=([\d.]+)", result.stdout)
    assert lines and int(lines.group(1)) == 100
    assert elapsed and float(elapsed.group(1)) < 10


def test_pip_install_returns_failure_and_keeps_diagnostics(pip_environment):
    result = _run_pip(pip_environment, pip_environment["invalid_wheel"])

    match = re.search(r"EXIT_CODE=(\d+)", result.stdout)
    assert match and int(match.group(1)) != 0
    assert pip_environment["invalid_wheel"].name in result.stdout

    source = INSTALL_PS1.read_text(encoding="utf-8")
    assert re.search(r"if \(\$pipExitCode -ne 0\)", source)
