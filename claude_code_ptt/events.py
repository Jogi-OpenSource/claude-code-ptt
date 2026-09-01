"""Optional event webhook: mirrors what the daemon is doing to an HTTP endpoint.

Off unless `event_webhook` names a URL in the config. Everything the daemon
reports goes out as one JSON object with a `kind` field:

  {"kind": "state", "state": "listening"|"thinking"|"speaking"|"idle",
   "level": 0.0-1.0}      the microphone/reply state, level only while recording
  {"kind": "speak", "text": ..., "session": ...}   a reply was queued for TTS

Delivery is fire-and-forget on a worker thread and failures are swallowed: a
listener that is slow, gone or broken must never stall recording or playback.
Only the oldest state is dropped from a backlog - a level from a second ago is
not worth delivering late (a listener animating a level meter wants "now", not
a faithful history), while every `speak` is kept because it carries text
nothing else will repeat.
"""
import json
import logging
import queue
import threading
import urllib.error
import urllib.request

log = logging.getLogger("claude_code_ptt")

TIMEOUT = 2.0                              # a listener gets this long, no more
QUEUE_MAX = 64

_url = ""
_queue: "queue.Queue[dict]" = queue.Queue(maxsize=QUEUE_MAX)
_worker: threading.Thread | None = None
_lock = threading.Lock()


def configure(url: str) -> None:
    """Point events at `url` (empty string turns them off)."""
    global _url, _worker
    with _lock:
        _url = (url or "").strip()
        if not _url or _worker is not None:
            return
        _worker = threading.Thread(target=_run, daemon=True)
        _worker.start()
        log.info("event webhook -> %s", _url)


def emit(kind: str, **fields) -> None:
    """Queue one event. Never raises, never blocks."""
    if not _url:
        return
    payload = {"kind": kind, **fields}
    try:
        _queue.put_nowait(payload)
    except queue.Full:
        # Backlog: the listener is not keeping up. Drop the oldest STATE
        # (superseded by this one anyway) rather than this event, so a
        # speak never falls out of a queue filled by level updates.
        _drop_oldest_state()
        try:
            _queue.put_nowait(payload)
        except queue.Full:
            log.debug("event webhook backlog, dropped %s", kind)


def _drop_oldest_state() -> None:
    kept = []
    dropped = False
    while True:
        try:
            item = _queue.get_nowait()
        except queue.Empty:
            break
        if not dropped and item.get("kind") == "state":
            dropped = True                 # this one goes
            continue
        kept.append(item)
    for item in kept:
        try:
            _queue.put_nowait(item)
        except queue.Full:
            break


def _run() -> None:
    while True:
        payload = _queue.get()
        try:
            _post(payload)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.debug("event webhook failed (%s): %s", payload.get("kind"), exc)


def _post(payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT):
        pass
