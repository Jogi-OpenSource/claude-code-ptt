"""Stop hook: report the end of a turn and confirm queued prompts.

Messages submitted while the session was busy never fire UserPromptSubmit -
they wait in the session's input queue and are consumed mid-turn. Their
delivery proof therefore happens here: when the turn ends, the tail of the
session transcript is reported to the daemon, which matches any prompt it is
still waiting on. The idle report afterwards starts the daemon's grace
window. Must never block or fail; failures land in confirm_hook.log.
"""
import json
import sys
import urllib.request

from .config import Config
from .confirm_hook import _note, hook_cwd

TAIL_LINES = 120
MAX_REPORTS = 8


def _post(port: int, path: str, body: dict) -> None:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=2).read()


def _user_texts(transcript_path: str) -> list[str]:
    texts: list[str] = []
    with open(transcript_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()[-TAIL_LINES:]
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if (entry.get("type") == "queue-operation"
                and entry.get("operation") == "enqueue"):
            # interjection queued mid-turn: stored as its own entry type,
            # not as a user message
            texts.append(str(entry.get("content") or ""))
            continue
        if entry.get("type") != "user":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(str(part.get("text") or ""))
    return texts


def main() -> None:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        port = Config.load().daemon_port
        cwd = hook_cwd(payload)
        transcript_path = str(payload.get("transcript_path") or "")
        if transcript_path:
            try:
                for text in _user_texts(transcript_path)[-MAX_REPORTS:]:
                    _post(port, "/prompt-received",
                          {"text": text, "cwd": cwd,
                           "transcript": transcript_path})
            except OSError:
                pass                       # transcript unreadable: skip
        _post(port, "/turn-state", {"cwd": cwd, "state": "idle",
                                    "transcript": transcript_path})
    except Exception as exc:               # noqa: BLE001
        _note(f"turn_hook FAILED: {exc!r}")


if __name__ == "__main__":
    main()
