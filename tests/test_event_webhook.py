"""The optional event webhook: off by default, fire-and-forget when on."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_code_ptt import events


@pytest.fixture(autouse=True)
def _reset_events():
    """Each test starts from a webhook that is off and an empty queue."""
    events._url = ""
    while not events._queue.empty():
        events._queue.get_nowait()
    yield
    events._url = ""


class _Collector:
    """A throwaway HTTP server that records the JSON bodies posted to it."""

    def __init__(self, status=200):
        self.received: list[dict] = []
        self.got = threading.Event()
        collector = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                collector.received.append(json.loads(self.rfile.read(length)))
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                collector.got.set()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever,
                         daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/event"

    def close(self):
        self._server.shutdown()


def test_emit_without_a_configured_url_does_nothing():
    events.emit("state", state="listening")
    assert events._queue.empty()


def test_configured_webhook_receives_the_payload():
    collector = _Collector()
    try:
        events.configure(collector.url)
        events.emit("speak", text="hello", session="work")
        assert collector.got.wait(5), "webhook never received the event"
    finally:
        collector.close()
    assert collector.received == [
        {"kind": "speak", "source": events.SOURCE,
         "text": "hello", "session": "work"}]


def test_a_failing_listener_never_raises_to_the_caller():
    # nothing listens on this port; emit must still return normally
    events.configure("http://127.0.0.1:1/event")
    events.emit("state", state="idle")
    events.emit("speak", text="still fine")


def test_a_full_queue_drops_state_before_speak():
    """A backlog of level updates must not push out a spoken reply: the
    text is the only thing nothing else will repeat."""
    events.configure("http://127.0.0.1:1/event")
    for _ in range(events.QUEUE_MAX):
        events._queue.put_nowait({"kind": "state", "state": "listening"})
    events.emit("speak", text="keep me")
    queued = []
    while not events._queue.empty():
        queued.append(events._queue.get_nowait())
    assert {"kind": "speak", "source": events.SOURCE,
            "text": "keep me"} in queued
    assert len(queued) == events.QUEUE_MAX
