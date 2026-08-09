"""Localhost HTTP API of the daemon, used by the MCP adapter and /ptt-here.

Endpoints (JSON):
  GET  /status          -> {recording, speaking, target, version}
  GET  /sessions        -> registered sessions
  POST /speak           -> body {"text": ...}; queues TTS playback
  POST /interrupt       -> stops TTS playback
  POST /register        -> body {"pid", "cwd", "static"?}; session announces itself
  POST /heartbeat       -> body {"pid": ...}; keeps a registration alive
  POST /unregister      -> body {"pid": ...}; session says goodbye
  POST /prompt-received -> body {"text": ...}; confirm hook reports a prompt
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
                    "target": daemon.target_hwnd(),
                    "version": __version__,
                })
            elif self.path == "/sessions":
                self._send(200, daemon.registry.list())
            else:
                self._send(404, {"error": "unknown path"})

        def do_POST(self):
            if self.path == "/speak":
                body = self._body()
                text = str(body.get("text", "")).strip()
                if not text:
                    self._send(400, {"error": "missing 'text'"})
                    return
                daemon.speaker.speak(text, int(body.get("pid") or 0))
                self._send(200, {"ok": True})
            elif self.path == "/interrupt":
                daemon.speaker.interrupt()
                self._send(200, {"ok": True})
            elif self.path == "/register":
                body = self._body()
                pid = int(body.get("pid") or 0)
                cwd = str(body.get("cwd") or "")
                if not pid:
                    self._send(400, {"error": "missing 'pid'"})
                    return
                info = daemon.registry.register(pid, cwd,
                                                bool(body.get("static")))
                self._send(200, {"ok": True, "session": info})
            elif self.path == "/unregister":
                daemon.registry.unregister(int(self._body().get("pid") or 0))
                self._send(200, {"ok": True})
            elif self.path == "/heartbeat":
                pid = int(self._body().get("pid") or 0)
                # always 200: "unknown" tells the adapter to re-register,
                # a 4xx would surface as an exception there instead
                self._send(200, {"ok": daemon.registry.heartbeat(pid)})
            elif self.path == "/prompt-received":
                text = str(self._body().get("text") or "")
                self._send(200, {"ok": daemon.confirm_received(text)})
            elif self.path == "/cue":
                from .cues import play_cue
                play_cue(str(self._body().get("type") or ""))
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "unknown path"})

    return Handler


def start(daemon, port: int) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(daemon))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("HTTP API on 127.0.0.1:%d", port)
