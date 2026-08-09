"""One-shot setup: register the MCP server and the two Claude Code hooks.

Run via `claude-code-ptt install` (or `python -m claude_code_ptt.installer`).
Idempotent: safe to run again after an update or a Python move - existing
entries are updated in place, nothing is duplicated.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

MCP_NAME = "jogi-ptt"
HOOK_TIMEOUT = 5
# event name -> module whose main() the hook runs
HOOKS = {"Stop": "turn_hook", "UserPromptSubmit": "confirm_hook"}


def _hook_command(module: str) -> str:
    return f'"{sys.executable}" -m claude_code_ptt.{module}'


def _merge_hook(settings: dict, event: str, module: str) -> str:
    """Insert or refresh one hook entry; returns what happened."""
    entries = settings.setdefault("hooks", {}).setdefault(event, [])
    marker = f"claude_code_ptt.{module}"
    wanted = _hook_command(module)
    for entry in entries:
        for hook in entry.get("hooks", []):
            if marker in str(hook.get("command", "")):
                if hook["command"] == wanted:
                    return "ok"
                hook["command"] = wanted       # stale interpreter path
                return "updated"
    entries.append({"hooks": [{"type": "command", "command": wanted,
                               "timeout": HOOK_TIMEOUT}]})
    return "added"


def _install_hooks() -> bool:
    path = Path.home() / ".claude" / "settings.json"
    settings = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            print(f"ERROR: {path} is not valid JSON - fix it and rerun.")
            return False
    changed = False
    for event, module in HOOKS.items():
        result = _merge_hook(settings, event, module)
        print(f"  hook {event} -> {module}: {result}")
        changed = changed or result != "ok"
    if changed:
        if path.exists():
            shutil.copy2(path, path.with_suffix(".json.ccptt-backup"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n",
                        encoding="utf-8")
        print(f"  wrote {path} (backup: settings.json.ccptt-backup)")
    return True


def _install_mcp() -> bool:
    claude = shutil.which("claude")
    if not claude:
        print("ERROR: `claude` not found in PATH - install Claude Code "
              "first (https://claude.com/claude-code), then rerun:\n"
              f'  "{sys.executable}" -m claude_code_ptt.installer')
        return False
    probe = subprocess.run([claude, "mcp", "get", MCP_NAME],
                           capture_output=True, text=True)
    if probe.returncode == 0:
        print(f"  MCP server `{MCP_NAME}`: already registered")
        return True
    adapter = Path(sys.executable).parent / "claude-code-ptt-mcp.exe"
    if not adapter.exists():
        adapter = adapter.with_suffix("")
    add = subprocess.run(
        [claude, "mcp", "add", "--scope", "user", MCP_NAME, "--",
         str(adapter)],
        capture_output=True, text=True)
    if add.returncode != 0:
        print(f"ERROR: claude mcp add failed:\n{add.stderr or add.stdout}")
        return False
    print(f"  MCP server `{MCP_NAME}`: registered (scope user)")
    return True


def _fetch_model() -> bool:
    """Pull the Whisper weights now, while a progress bar is on screen.

    Left to itself the model downloads on the very first recording, where the
    user sees nothing happen for several minutes and assumes it is broken.
    """
    from .config import Config

    name = Config.load().whisper_model
    print(f"\nDownloading the Whisper model `{name}` (several hundred MB).")
    print("This is a one-off; progress is shown below.")
    try:
        from faster_whisper import WhisperModel
        WhisperModel(name, device="cpu", compute_type="int8")
    except Exception as exc:                      # network, disk, bad model id
        print(f"  WARNING: download failed ({exc}).")
        return False
    print(f"  model `{name}`: ready")
    return True


def main() -> int:
    print("claude-code-ptt setup")
    ok = _install_mcp()
    ok = _install_hooks() and ok
    if not ok:
        return 1
    retry = "" if _fetch_model() else (
        "\nThe Whisper model still has to download on your first "
        "recording - give it a few minutes.")
    print("\nDone. Start a NEW Claude Code session, then press Ctrl+M "
          "(default) and speak." + retry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
