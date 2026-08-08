"""Thin MCP server: connects a Claude Code session to the local PTT daemon.

Install once, valid for every session of the user:
  claude mcp add --scope user ptt -- claude-code-ptt-mcp

Starts the daemon automatically if it is not running yet.
"""
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
        "global hotkey; transcripts arrive prefixed with a microphone emoji. "
        "When you see such input, ALWAYS answer via the ptt_speak tool as "
        "well (short, spoken-style summary) in the language the user speaks."
    ),
)

_config = Config.load()
_BASE = f"http://127.0.0.1:{_config.daemon_port}"


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
    _request("/speak", {"text": text})
    return "queued"


@mcp.tool()
def ptt_status() -> dict:
    """Current PTT daemon status (recording, speaking, target window)."""
    _ensure_daemon()
    return _request("/status")


def _register_session() -> None:
    """Announce this session to the daemon and keep it alive with heartbeats.

    The floating overlay lists every registered session; a dead adapter stops
    heartbeating and the session drops out of the list automatically.
    """
    payload = {"pid": os.getpid(), "cwd": os.getcwd()}

    def loop():
        while True:
            try:
                known = _request("/heartbeat", {"pid": payload["pid"]})
                if not known.get("ok"):
                    _request("/register", payload)
            except (urllib.error.URLError, OSError):
                pass                        # daemon restarts re-register us
            time.sleep(10)

    try:
        _request("/register", payload)
    except (urllib.error.URLError, OSError):
        pass
    threading.Thread(target=loop, daemon=True).start()


def main() -> None:
    _ensure_daemon()
    _register_session()
    mcp.run()


if __name__ == "__main__":
    main()
