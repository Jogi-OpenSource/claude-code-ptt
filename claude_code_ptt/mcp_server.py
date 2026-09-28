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

from . import session_port
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
_port = session_port.resolve(_config.daemon_port)
_windows_session = session_port.session_id()
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
        f"http://127.0.0.1:{_port}{path}", data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None or path != "/status" else "GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _found_our_daemon() -> bool:
    """Is a daemon of OUR Windows session listening for us?

    Re-reads the port note before asking, because the port this adapter
    resolved at import time goes stale: the daemon of this session may
    have had to bind a different one since. Without the re-read, this one
    would keep looking at the port the stranger owns and spawn daemon
    after daemon.

    A daemon started by another logged-on account answers on the same
    loopback port, but its overlay lives on a desktop we cannot see -
    talking to it would leave this session mute. A daemon too old to name
    its session counts as ours: on a machine with one account it is."""
    global _port
    _port = session_port.resolve(_config.daemon_port)
    try:
        status = _request("/status")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    if "version" not in status:            # somebody else's HTTP server
        return False
    return status.get("session", _windows_session) == _windows_session


def _ensure_daemon() -> None:
    # Adapters come up in bunches - one per Claude session the user opens -
    # so several of them spawn a daemon at once. They sort that out among
    # themselves (see session_port.claim_instance): one becomes the daemon,
    # the rest stop before taking anything, and all adapters end up at the
    # port that one noted down.
    if _found_our_daemon():
        return
    # CREATE_NO_WINDOW, not DETACHED_PROCESS: a detached venv launcher has no
    # console, so the real interpreter it starts allocates a fresh, visible
    # one. A hidden console is inherited instead. Logs still go to daemon.log.
    subprocess.Popen(
        [sys.executable, "-m", "claude_code_ptt.daemon"],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    for _ in range(50):                    # first Whisper download can be slow
        time.sleep(0.2)
        if _found_our_daemon():
            return
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


def _heartbeat(payload: dict) -> None:
    """Keep this session's registration alive for one round.

    Goes through _found_our_daemon rather than straight to the port this
    adapter last used, because that port goes stale between rounds: our
    daemon restarts onto the configured one as soon as the stranger who
    pushed it aside logs off. Heartbeating on where it stood would drop
    this session out of the overlay until the next tool call rediscovers
    it - and if the stranger's daemon has since taken that port, register
    would list this session on a desktop we cannot see."""
    if not _found_our_daemon():
        return                              # daemon restarts re-register us
    try:
        if not _request("/heartbeat", {"pid": payload["pid"]}).get("ok"):
            _request("/register", payload)
    except (urllib.error.URLError, OSError):
        pass                                # next round tries again


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
            _heartbeat(payload)
            time.sleep(10)

    def goodbye():
        try:
            _request("/unregister", {"pid": payload["pid"]})
        except (urllib.error.URLError, OSError):
            pass                            # reaper cleans up eventually

    _heartbeat(payload)                     # into the overlay right away
    atexit.register(goodbye)
    threading.Thread(target=loop, daemon=True).start()


def main() -> None:
    _ensure_daemon()
    _register_session()
    mcp.run()


if __name__ == "__main__":
    main()
