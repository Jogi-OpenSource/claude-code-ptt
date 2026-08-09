"""UserPromptSubmit hook: report every processed prompt to the PTT daemon.

Installed alongside the MCP adapter, this is the delivery proof the overlay
relies on: only when the injected transcript comes back through this hook is
it really being processed by the session - text stuck in the input line
never reaches this point. Must never block or fail the user's prompt; every
run leaves a trace in confirm_hook.log so silent failures stay diagnosable.
"""
import json
import sys
import time
import urllib.request

from .config import Config, config_dir


def _note(message: str) -> None:
    try:
        with open(config_dir() / "confirm_hook.log", "a",
                  encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def main() -> None:
    try:
        # Hook input is UTF-8 JSON; decode explicitly - text-mode stdin
        # falls back to the locale codepage on Windows and garbles the
        # prompt, which breaks the daemon's delivery match.
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        prompt = str(payload.get("prompt") or "")
        if not prompt:
            _note("empty prompt, nothing to report")
            return
        port = Config.load().daemon_port
        body = json.dumps({"text": prompt,
                           "cwd": str(payload.get("cwd") or "")},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/prompt-received", data=body,
            headers={"Content-Type": "application/json"})
        response = urllib.request.urlopen(req, timeout=2).read().decode()
        _note(f"posted {len(prompt)} chars, head={prompt[:40]!r}, "
              f"response={response}")
    except Exception as exc:               # noqa: BLE001
        _note(f"FAILED: {exc!r}")          # never break the user's prompt


if __name__ == "__main__":
    main()
