"""Tests for Add-ToUserPath in install.ps1.

The persistent PATH entry is what makes `claude` work in a console opened
*after* the install - a Windows Sandbox run on 09.08. produced "claude is not
recognized" in a fresh window, so this logic gets a regression test.

install.ps1 is a flat script: dot-sourcing it would run the whole installer.
The harness below therefore lifts the single function out by name and points
it at a throwaway registry key, so nothing here touches HKCU:\\Environment.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="install.ps1 is Windows-only"
)

INSTALL_PS1 = Path(__file__).resolve().parent.parent / "install.ps1"
TEST_KEY = r"HKCU:\Software\ccptt-pathtest"

HARNESS = r"""
param([string]$Script, [string]$Key, [string]$CasesJson, [string]$OutJson)
$ErrorActionPreference = "Stop"

$lines = Get-Content -LiteralPath $Script
$start = ($lines | Select-String -Pattern '^function Add-ToUserPath \{' |
          Select-Object -First 1).LineNumber - 1
$end = $start
while ($lines[$end] -ne "}") { $end++ }
# Same function, different hive - and no WM_SETTINGCHANGE storm during a test
# run; the broadcast carries no state worth asserting on.
function Publish-EnvironmentChange { }
Invoke-Expression (($lines[$start..$end] -join "`n").Replace('HKCU:\Environment', $Key))

$results = @{}
foreach ($case in (Get-Content -LiteralPath $CasesJson -Raw | ConvertFrom-Json).PSObject.Properties) {
    New-Item -Path $Key -Force | Out-Null
    if ($null -eq $case.Value.before) {
        Remove-ItemProperty -Path $Key -Name Path -ErrorAction SilentlyContinue
    } else {
        Set-ItemProperty -Path $Key -Name Path -Value $case.Value.before -Type ExpandString
    }
    Add-ToUserPath $case.Value.dir *> $null
    $k = Get-Item $Key
    $results[$case.Name] = @{
        # Raw, so an accidentally expanded %VAR% shows up instead of hiding.
        after = $k.GetValue("Path", "", "DoNotExpandEnvironmentNames")
        kind  = $k.GetValueKind("Path").ToString()
    }
}
Remove-Item -Path $Key -Recurse -Force
$results | ConvertTo-Json | Set-Content -LiteralPath $OutJson
"""


@pytest.fixture(scope="module")
def cases(tmp_path_factory):
    """Run every case through the real function once, return raw results."""
    tmp = tmp_path_factory.mktemp("pathtest")
    binary_dir = tmp / "bin"
    binary_dir.mkdir()
    plain = str(binary_dir)
    # An existing directory the user's PATH can also name via a variable -
    # %SystemRoot% is on every Windows.
    system32 = os.path.join(os.environ["SystemRoot"], "system32")

    wanted = {
        "no_value": {"before": None, "dir": plain},
        "empty": {"before": "", "dir": plain},
        "absent": {"before": r"C:\a;C:\b", "dir": plain},
        "present": {"before": rf"C:\a;{plain};C:\b", "dir": plain},
        "trailing_backslash": {"before": "C:\\a;" + plain + "\\", "dir": plain},
        "trailing_semicolon": {"before": r"C:\a;C:\b;", "dir": plain},
        "other_case": {"before": rf"C:\a;{plain.upper()}", "dir": plain},
        "keeps_vars": {"before": r"%SystemRoot%\notthere;C:\a", "dir": plain},
        "var_names_entry": {"before": r"%SystemRoot%\system32", "dir": system32},
    }
    harness = tmp / "harness.ps1"
    harness.write_text(HARNESS, encoding="utf-8")
    cases_json = tmp / "cases.json"
    cases_json.write_text(json.dumps(wanted), encoding="utf-8")
    out_json = tmp / "out.json"

    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(harness), "-Script", str(INSTALL_PS1), "-Key", TEST_KEY,
         "-CasesJson", str(cases_json), "-OutJson", str(out_json)],
        check=True, capture_output=True,
    )
    results = json.loads(out_json.read_text(encoding="utf-8"))
    return {"results": results, "wanted": wanted, "dir": plain}


def _after(cases, name):
    return cases["results"][name]["after"]


def test_appends_when_the_path_is_empty_or_missing(cases):
    """A brand-new profile has no Path value at all - no leading semicolon."""
    assert _after(cases, "no_value") == cases["dir"]
    assert _after(cases, "empty") == cases["dir"]


def test_appends_once_and_only_once(cases):
    """Rerunning the installer must not grow the PATH every time."""
    assert _after(cases, "absent") == rf"C:\a;C:\b;{cases['dir']}"
    for name in ("present", "trailing_backslash", "other_case", "var_names_entry"):
        assert _after(cases, name) == cases["wanted"][name]["before"], name


def test_no_double_semicolon(cases):
    assert _after(cases, "trailing_semicolon") == rf"C:\a;C:\b;{cases['dir']}"


def test_percent_variables_are_written_back_unexpanded(cases):
    """Expanding them would freeze another user's %VARS% into literals."""
    assert _after(cases, "keeps_vars") == rf"%SystemRoot%\notthere;C:\a;{cases['dir']}"
    assert all(r["kind"] == "ExpandString" for r in cases["results"].values())
