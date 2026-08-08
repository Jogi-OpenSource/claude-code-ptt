"""UserPromptSubmit hook: report every processed prompt to the PTT daemon.

Installed alongside the MCP adapter, this is the delivery proof the overlay
relies on: only when the injected transcript comes back through this hook is
it really being processed by the session - text stuck in the input line
never reaches this point. Must never block or fail the user's prompt.
"""
import json
import sys
import urllib.request

from .config import Config


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        prompt = str(payload.get("prompt") or "")
        if not prompt:
            return
        port = Config.load().daemon_port
        body = json.dumps({"text": prompt}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/prompt-received", data=body,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:                      # noqa: BLE001
        pass                               # never break the user's prompt


if __name__ == "__main__":
    main()
