"""Thin MCP server: connects a Claude Code session to the local PTT daemon.

Install once, valid for every session of the user:
  claude mcp add --scope user ptt -- claude-code-ptt-mcp

Starts the daemon automatically if it is not running yet.
"""
import atexit
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

from .config import Config

mcp = FastMCP(
    "ptt",
    instructions=(
        "Push-to-talk voice server. The user toggles the microphone with a "
        "global hotkey; transcripts arrive prefixed with \"[mic] \". "
        "When you see such input, ALWAYS answer via the ptt_speak tool as "
        "well (short, spoken-style summary) in the language the user speaks."
    ),
)

_config = Config.load()
_BASE = f"http://127.0.0.1:{_config.daemon_port}"
_session_pid_cache = 0


def _my_session_pid() -> int:
    global _session_pid_cache
    if not _session_pid_cache:
        _session_pid_cache = _session_pid()
    return _session_pid_cache


def _request(path: str, payload: dict | None = None) -> dict:
    import json
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        _BASE + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None or path != "/status" else "GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _ensure_daemon() -> None:
    try:
        _request("/status")
        return
    except (urllib.error.URLError, OSError):
        pass
    subprocess.Popen(
        [sys.executable, "-m", "claude_code_ptt.daemon"],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    for _ in range(50):                    # first Whisper download can be slow
        time.sleep(0.2)
        try:
            _request("/status")
            return
        except (urllib.error.URLError, OSError):
            continue
    raise RuntimeError("PTT daemon did not come up")


@mcp.tool()
def ptt_speak(text: str) -> str:
    """Speak text aloud to the user (queued, non-blocking)."""
    _ensure_daemon()
    _request("/speak", {"text": text, "pid": _my_session_pid()})
    return "queued"


@mcp.tool()
def ptt_status() -> dict:
    """Current PTT daemon status (recording, speaking, target window)."""
    _ensure_daemon()
    return _request("/status")


def _session_pid() -> int:
    """PID of the Claude session this adapter belongs to.

    Claude Code spawns the adapter as its child (possibly through launcher
    wrappers). Walking up the OS process tree, every rung that is one of
    OUR OWN executables gets skipped; the first foreign ancestor is the
    session process itself. Falls back to the adapter's own pid."""
    own = {"python.exe", "pythonw.exe", "claude-code-ptt-mcp.exe"}
    try:
        from .sessions import process_tree
        tree = process_tree()
        pid = os.getpid()
        for _ in range(8):
            parent, _ = tree.get(pid, (0, ""))
            if not parent:
                break
            _, parent_name = tree.get(parent, (0, ""))
            if parent_name not in own:
                return parent
            pid = parent
    except Exception:                      # noqa: BLE001
        pass
    return os.getpid()


def _register_session() -> None:
    """Announce this session to the daemon and keep it alive with heartbeats.

    The floating overlay lists every registered session; a dead adapter stops
    heartbeating and the session drops out of the list automatically. The
    registered pid is the SESSION's (claude), not the adapter's - the window
    search starts from it, and it is what the user identifies a session by.
    """
    payload = {"pid": _my_session_pid(), "cwd": os.getcwd()}

    def loop():
        while True:
            try:
                known = _request("/heartbeat", {"pid": payload["pid"]})
                if not known.get("ok"):
                    _request("/register", payload)
            except (urllib.error.URLError, OSError):
                pass                        # daemon restarts re-register us
            time.sleep(10)

    def goodbye():
        try:
            _request("/unregister", {"pid": payload["pid"]})
        except (urllib.error.URLError, OSError):
            pass                            # reaper cleans up eventually

    try:
        _request("/register", payload)
    except (urllib.error.URLError, OSError):
        pass
    atexit.register(goodbye)
    threading.Thread(target=loop, daemon=True).start()


def main() -> None:
    _ensure_daemon()
    _register_session()
    mcp.run()


if __name__ == "__main__":
    main()
