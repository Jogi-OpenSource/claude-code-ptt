"""Tests for hook console scripts and installer command selection."""
import sys
import tomllib
from pathlib import Path

from claude_code_ptt import installer


HOOK_ENTRY_POINTS = {
    "claude-code-ptt-turn-hook": "claude_code_ptt.turn_hook:main",
    "claude-code-ptt-confirm-hook": "claude_code_ptt.confirm_hook:main",
}



def test_package_exposes_both_hook_entry_points():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]

    for name, target in HOOK_ENTRY_POINTS.items():
        assert scripts.get(name) == target


def test_hook_candidates_prefer_console_executable_and_keep_module_forms(
        monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    hook = scripts / "claude-code-ptt-confirm-hook.exe"
    hook.write_bytes(b"")
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    short_hook = "C:/SHORT/CONFIR~1.EXE"
    short_python = "C:/SHORT/PYTHON.EXE"

    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(
        installer,
        "_script_path",
        lambda name: hook if name.endswith("confirm-hook") else None,
    )
    monkeypatch.setattr(
        installer,
        "_short_path",
        lambda path: short_hook if path == str(hook) else short_python,
    )

    candidates = installer._hook_candidates("confirm_hook")
    hook_path = str(hook).replace("\\", "/")
    python_path = str(python).replace("\\", "/")

    assert candidates[:2] == [hook_path, short_hook]
    for module_form in (
        f"{python_path} -m claude_code_ptt.confirm_hook",
        f"{short_python} -m claude_code_ptt.confirm_hook",
        f'cmd /c ""{python}" -m claude_code_ptt.confirm_hook"',
        f'"{python}" -m claude_code_ptt.confirm_hook',
        f'& "{python}" -m claude_code_ptt.confirm_hook',
    ):
        assert module_form in candidates


def test_working_console_form_falls_back_when_other_hook_script_is_missing(
        monkeypatch):
    confirm = Path("C:/Tools/claude-code-ptt-confirm-hook.exe")
    monkeypatch.setattr(
        installer,
        "_script_path",
        lambda name: confirm if name.endswith("confirm-hook") else None,
    )
    monkeypatch.setattr(installer, "_short_path", lambda _path: "")
    monkeypatch.setattr(installer, "_working_form", lambda: ("console", 4))

    assert installer._hook_command("turn_hook") == (
        f'{sys.executable.replace("\\", "/")} -m claude_code_ptt.turn_hook')


def test_merge_hook_migrates_module_form_idempotently(monkeypatch):
    wanted = "C:/Tools/claude-code-ptt-confirm-hook.exe"
    settings = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{
                    "type": "command",
                    "command": "C:/Python/python.exe -m claude_code_ptt.confirm_hook",
                    "timeout": 5,
                }]}
            ]
        }
    }
    monkeypatch.setattr(installer, "_hook_command", lambda _module: wanted)

    assert installer._merge_hook(
        settings, "UserPromptSubmit", "confirm_hook") == "updated"
    assert installer._merge_hook(
        settings, "UserPromptSubmit", "confirm_hook") == "ok"
    assert len(settings["hooks"]["UserPromptSubmit"]) == 1


def test_merge_hook_recognizes_existing_console_executable(monkeypatch):
    wanted = "C:/Tools/claude-code-ptt-confirm-hook.exe"
    stale = "C:/Old/claude-code-ptt-confirm-hook.exe"
    settings = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": stale, "timeout": 5}]}
            ]
        }
    }
    monkeypatch.setattr(installer, "_hook_command", lambda _module: wanted)

    result = installer._merge_hook(settings, "UserPromptSubmit", "confirm_hook")

    hooks = settings["hooks"]["UserPromptSubmit"]
    assert result == "updated"
    assert len(hooks) == 1
    assert hooks[0]["hooks"][0]["command"] == wanted
