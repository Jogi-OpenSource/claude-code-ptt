"""One-shot setup: register the MCP server and the two Claude Code hooks.

Run via `claude-code-ptt install` (or `python -m claude_code_ptt.installer`).
Idempotent: safe to run again after an update or a Python move - existing
entries are updated in place, nothing is duplicated.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from functools import lru_cache
from pathlib import Path

from .config import config_dir

# Set before anything imports huggingface_hub, which reads it once at import.
# Its xet downloader assembles the file where a size check cannot see it, so
# the counter sits frozen for half a minute and then jumps. On a 480 MB model
# xet saved 4 seconds out of 50 - not worth an install that looks dead.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
# Same file, second symptom: the Hub answers a download with notices of its
# own ("You are sending unauthenticated requests to the HF Hub"), which the
# library logs mid-line - right through the self-overwriting \r progress line
# below, which then reads as garbage. Only the logger is quietened: what
# actually fails still raises, and _fetch_model() reports it.
os.environ.setdefault("HF_HUB_VERBOSITY", "error")

MCP_NAME = "jogi-ptt"
HOOK_TIMEOUT = 5
# event name -> module whose main() the hook runs
HOOKS = {"Stop": "turn_hook", "UserPromptSubmit": "confirm_hook"}
# Prompt text the hook probe sends: recognisable in the log, and harmless if
# a running daemon sees it (no transcript is waiting on it).
HOOK_PROBE_PROMPT = "claude-code-ptt install probe"


def _short_path(path: str) -> str:
    """The 8.3 spelling of a path, or "" where Windows has none.

    Rescues installs under "C:\\Program Files\\...": the short form carries no
    spaces, so the command needs no quotes and every shell parses it alike.
    Short-name creation can be switched off system-wide, hence the empty
    fallback - the caller just drops the candidate then.
    """
    buffer = ctypes.create_unicode_buffer(512)
    length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, 512)
    short = buffer.value if length else ""
    return short if short and " " not in short else ""


def _hook_candidates(module: str) -> list[str]:
    """Every spelling of "run this module" worth trying, best first.

    Claude Code hands hook commands to whichever shell it inherited - cmd,
    PowerShell or bash have all been observed - and the three disagree about
    quoting. A bare quoted path is a command in cmd but a string literal in
    PowerShell; a cmd /c wrapper fixes PowerShell and breaks bash, which eats
    the backslashes. None of them is right everywhere, so the installer tries
    them and keeps the one that provably runs (see _hook_command).
    """
    candidates = []
    for path in (sys.executable, _short_path(sys.executable)):
        if path and " " not in path:
            # Space-free path: no quoting needed, so no shell can disagree.
            forward = path.replace("\\", "/")
            candidates.append(f"{forward} -m claude_code_ptt.{module}")
    exe = sys.executable
    candidates += [
        f'cmd /c ""{exe}" -m claude_code_ptt.{module}"',
        f'"{exe}" -m claude_code_ptt.{module}',
        f'& "{exe}" -m claude_code_ptt.{module}',
    ]
    return candidates


def _shells() -> list[list[str]]:
    """Every shell present that Claude Code might hand a hook command to."""
    found = []
    for parts in (["cmd", "/c"], ["powershell", "-NoProfile", "-Command"],
                  ["bash", "-c"]):
        if shutil.which(parts[0]):
            found.append(parts)
    return found


def _hook_runs(command: str) -> bool:
    """True if the command reaches the module in every shell on this machine.

    The hook appends to its own log on every run, successful or not, so a
    fresh line proves the shell parsed the command and Python started. All
    shells have to agree, because which one Claude Code uses is not ours to
    choose - and each of the earlier quoting bugs worked in one and died in
    another.
    """
    log = config_dir() / "confirm_hook.log"
    payload = json.dumps({"prompt": HOOK_PROBE_PROMPT, "cwd": ""})
    for shell in _shells():
        before = log.stat().st_size if log.exists() else 0
        try:
            subprocess.run(shell + [command], input=payload.encode("utf-8"),
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return False
        after = log.stat().st_size if log.exists() else 0
        if after <= before:
            return False
    return True


@lru_cache(maxsize=1)
def _working_form() -> int:
    """Index of the first candidate that provably runs, probed once."""
    for index, candidate in enumerate(_hook_candidates("confirm_hook")):
        if _hook_runs(candidate):
            return index
    # Nothing provably ran - keep the plain quoted form rather than none at
    # all, and say so, because delivery confirmation is the visible casualty.
    print("  warning: no hook command form could be verified on this shell; "
          "delivery confirmation may stay silent")
    return -2


def _hook_command(module: str) -> str:
    return _hook_candidates(module)[_working_form()]


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


def _adapter_path() -> Path | None:
    """Locate the MCP adapter executable pip installed for us.

    Not `Path(sys.executable).parent`: that only holds true inside a venv. A
    normal Windows install keeps python.exe in the root and its scripts in
    Scripts\\, so that guess registered a path that does not exist and Claude
    Code reported "Failed to connect" with nothing to go on.
    """
    candidates = [sysconfig.get_path("scripts"),
                  sysconfig.get_path("scripts", scheme="nt_user"),
                  str(Path(sys.executable).parent)]
    for directory in candidates:
        if not directory:
            continue
        for name in ("claude-code-ptt-mcp.exe", "claude-code-ptt-mcp"):
            candidate = Path(directory) / name
            if candidate.exists():
                return candidate
    found = shutil.which("claude-code-ptt-mcp")
    return Path(found) if found else None


def _install_mcp() -> bool:
    claude = shutil.which("claude")
    if not claude:
        print("ERROR: `claude` not found in PATH - install Claude Code "
              "first (https://claude.com/claude-code), then rerun:\n"
              f'  "{sys.executable}" -m claude_code_ptt.installer')
        return False
    adapter = _adapter_path()
    if adapter is None:
        print("ERROR: claude-code-ptt-mcp was not found next to this Python.\n"
              "  Reinstall the package and rerun:\n"
              f'  "{sys.executable}" -m pip install --force-reinstall '
              "https://github.com/Jogi-OpenSource/claude-code-ptt/archive/main.zip")
        return False
    probe = subprocess.run([claude, "mcp", "get", MCP_NAME],
                           capture_output=True, text=True)
    if probe.returncode == 0:
        if str(adapter) in probe.stdout:
            print(f"  MCP server `{MCP_NAME}`: already registered")
            return True
        # An entry pointing somewhere else is worse than none: Claude Code
        # reports only "Failed to connect", and rerunning the installer used
        # to see a registration and leave it alone. Replace it.
        print(f"  MCP server `{MCP_NAME}`: re-registering (stale path)")
        subprocess.run([claude, "mcp", "remove", "--scope", "user", MCP_NAME],
                       capture_output=True, text=True)
    add = subprocess.run(
        [claude, "mcp", "add", "--scope", "user", MCP_NAME, "--",
         str(adapter)],
        capture_output=True, text=True)
    if add.returncode != 0:
        print(f"ERROR: claude mcp add failed:\n{add.stderr or add.stdout}")
        return False
    print(f"  MCP server `{MCP_NAME}`: registered (scope user)")
    return True


def _cache_bytes(cache: str) -> int:
    total = 0
    for root, _dirs, names in os.walk(cache):
        for name in names:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:                       # deleted mid-walk
                continue
    return total


def _report_progress(cache: str, stop: threading.Event) -> None:
    """Report the download by what lands on disk.

    huggingface_hub prints nothing while it pulls half a gigabyte, and a
    silent installer is the one users kill. Filenames are no help here - the
    cache stores blobs under their hash - so report megabytes and rate.
    """
    base = _cache_bytes(cache)
    start = time.monotonic()
    while not stop.wait(1.0):
        done = (_cache_bytes(cache) - base) / 1e6
        seconds = time.monotonic() - start
        line = f"  {done:,.0f} MB | {int(seconds)}s | {done / seconds:,.1f} MB/s"
        print("\r" + line.ljust(60), end="", flush=True)
    print("\r" + " " * 60 + "\r", end="", flush=True)


def _model_cached(name: str) -> bool:
    try:
        from huggingface_hub import scan_cache_dir
        # endswith, not `in`: a cached `large-v3-turbo` must not pass for
        # `large-v3`, and `small.en` is not `small`.
        return any(repo.repo_id.endswith(f"faster-whisper-{name}")
                   for repo in scan_cache_dir().repos)
    except Exception:                             # no cache dir yet, old hub
        return False


def _fetch_model() -> bool:
    """Pull the Whisper weights now, while a progress bar is on screen.

    Left to itself the model downloads on the very first recording, where the
    user sees nothing happen for several minutes and assumes it is broken.
    """
    from .config import Config

    name = Config.load().whisper_model
    if _model_cached(name):
        print(f"\n  Whisper model `{name}`: already downloaded")
        return True
    print(f"\nDownloading the Whisper model `{name}` (several hundred MB).")
    print("This is a one-off; progress is shown below.")
    from huggingface_hub.constants import HF_HOME
    stop = threading.Event()
    reporter = threading.Thread(target=_report_progress,
                                args=(HF_HOME, stop), daemon=True)
    reporter.start()
    try:
        from faster_whisper import WhisperModel
        WhisperModel(name, device="cpu", compute_type="int8")
    except Exception as exc:                      # network, disk, bad model id
        print(f"  WARNING: download failed ({exc}).")
        return False
    finally:
        stop.set()
        reporter.join(timeout=2)
    print(f"  model `{name}`: ready")
    return True


def _check_voice() -> None:
    """Confirm the configured edge-tts voice exists, before it is needed.

    A typo here otherwise stays invisible until the first spoken reply fails.
    """
    import asyncio

    from .config import Config

    wanted = Config.load().tts_voice
    try:
        import edge_tts
        names = {v["ShortName"] for v in asyncio.run(edge_tts.list_voices())}
    except Exception as exc:                      # offline, service change
        print(f"  voice `{wanted}`: could not verify ({exc})")
        return
    if wanted in names:
        print(f"  voice `{wanted}`: available")
    else:
        print(f"  WARNING: voice `{wanted}` is not offered by edge-tts.")
        print("  Spoken replies will fail until you set another one in "
              "%APPDATA%/claude-code-ptt/config.json")


def main() -> int:
    print("claude-code-ptt setup")
    ok = _install_mcp()
    ok = _install_hooks() and ok
    if not ok:
        return 1
    have_model = _fetch_model()
    _check_voice()
    retry = "" if have_model else (
        "\nThe Whisper model still has to download on your first "
        "recording - give it a few minutes.")
    print("\nDone. Start a NEW Claude Code session, then press Ctrl+M "
          "(default) and speak." + retry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
