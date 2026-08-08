"""Localhost HTTP API of the daemon, used by the MCP adapter and /ptt-here.

Endpoints (JSON):
  GET  /status          -> {recording, speaking, target, version}
  POST /speak           -> body {"text": ...}; queues TTS playback
  POST /interrupt       -> stops TTS playback
  POST /pin-foreground  -> pins the current foreground window as target
  POST /unpin           -> back to automatic focus tracking
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__

log = logging.getLogger("claude_code_ptt")


def make_handler(daemon):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                return {}

        def do_GET(self):
            if self.path == "/status":
                self._send(200, {
                    "recording": daemon.recorder.recording,
                    "speaking": daemon.speaker.playing,
                    "target": daemon.tracker.target,
                    "version": __version__,
                })
            else:
                self._send(404, {"error": "unknown path"})

        def do_POST(self):
            if self.path == "/speak":
                text = str(self._body().get("text", "")).strip()
                if not text:
                    self._send(400, {"error": "missing 'text'"})
                    return
                daemon.speaker.speak(text)
                self._send(200, {"ok": True})
            elif self.path == "/interrupt":
                daemon.speaker.interrupt()
                self._send(200, {"ok": True})
            elif self.path == "/pin-foreground":
                hwnd = daemon.pin_foreground()
                self._send(200, {"ok": bool(hwnd), "target": hwnd})
            elif self.path == "/unpin":
                daemon.tracker.unpin()
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "unknown path"})

    return Handler


def start(daemon, port: int) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(daemon))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("HTTP API on 127.0.0.1:%d", port)
