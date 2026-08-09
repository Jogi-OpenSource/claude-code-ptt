"""Tests for claude_code_ptt.installer._adapter_path().

_adapter_path() has to work on two very different Windows Python layouts:

- a venv, where python.exe lives IN Scripts\\ (sysconfig's own scripts path
  and sys.executable's parent coincide)
- a normal python.org install, where python.exe sits at the root and the
  pip-installed console scripts land in a separate Scripts\\ subdirectory
  (sysconfig's scripts path and sys.executable's parent differ)

The historical bug used only `Path(sys.executable).parent`, which happens to
be right for the first layout and wrong for the second - see commit
609469e, found via `claude mcp list` in a fresh Windows Sandbox reporting
`jogi-ptt: ...\\Python312\\claude-code-ptt-mcp - Failed to connect` (adapter
actually sat in `...\\Python312\\Scripts\\claude-code-ptt-mcp.exe`).

These tests build both layouts under tmp_path - no real Python installation,
no real `claude` CLI, nothing outside the temp dir - and assert the adapter
is found either way, plus that a genuinely absent adapter resolves to None.
"""
import shutil
import sys
import sysconfig

from claude_code_ptt import installer

ADAPTER = "claude-code-ptt-mcp.exe"


def _patch_scripts_paths(monkeypatch, scripts_dir, nt_user_dir=None):
    """Make sysconfig.get_path("scripts", ...) return our fake directories."""
    def fake_get_path(name, scheme=None, *args, **kwargs):
        assert name == "scripts"
        if scheme == "nt_user":
            return str(nt_user_dir) if nt_user_dir else ""
        return str(scripts_dir) if scripts_dir else ""
    monkeypatch.setattr(sysconfig, "get_path", fake_get_path)


def test_venv_layout_finds_adapter_next_to_python(monkeypatch, tmp_path):
    """venv: python.exe AND the adapter both live in Scripts\\."""
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"")
    (scripts / ADAPTER).write_bytes(b"")

    _patch_scripts_paths(monkeypatch, scripts)
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert installer._adapter_path() == scripts / ADAPTER


def test_normal_install_finds_adapter_in_separate_scripts_dir(monkeypatch, tmp_path):
    """python.org install: python.exe at the root, adapter in .../Scripts\\.

    This is the exact layout commit 609469e fixed: `Path(sys.executable).
    parent` alone points at the root, where the adapter does not exist -
    only sysconfig's "scripts" path lands in the right directory.
    """
    root = tmp_path / "Python312"
    scripts = root / "Scripts"
    scripts.mkdir(parents=True)
    (root / "python.exe").write_bytes(b"")
    (scripts / ADAPTER).write_bytes(b"")

    _patch_scripts_paths(monkeypatch, scripts)
    monkeypatch.setattr(sys, "executable", str(root / "python.exe"))
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert installer._adapter_path() == scripts / ADAPTER


def test_missing_adapter_returns_none(monkeypatch, tmp_path):
    """Nowhere to find it: sysconfig paths empty, PATH empty, no fallback."""
    empty = tmp_path / "nothing_here"
    empty.mkdir()

    _patch_scripts_paths(monkeypatch, empty, nt_user_dir=None)
    monkeypatch.setattr(
        sys, "executable", str(tmp_path / "also_nothing" / "python.exe")
    )
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert installer._adapter_path() is None
