"""Stop hook: report the end of a turn to the PTT daemon.

The daemon watches the session transcript itself while a delivery is
pending, so this hook only flips the session back to idle (starting the
grace window for anything still unconfirmed). Must never block or fail;
failures land in confirm_hook.log.
"""
import json
import sys
import urllib.request

from .config import Config
from .confirm_hook import _note, hook_cwd


def main() -> None:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        body = json.dumps(
            {"cwd": hook_cwd(payload), "state": "idle",
             "transcript": str(payload.get("transcript_path") or "")},
            ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{Config.load().daemon_port}/turn-state",
            data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
    except Exception as exc:               # noqa: BLE001
        _note(f"turn_hook FAILED: {exc!r}")


if __name__ == "__main__":
    main()
