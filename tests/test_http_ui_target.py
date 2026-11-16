"""Tests for the front-end endpoints of the HTTP API: GET /ui and POST /target.

A front end other than the built-in overlay needs two things from the
daemon: everything the overlay shows (phase, target, speaking session,
session list) and a way to choose the target session. Both go through the
same daemon methods the overlay uses.

No running daemon: the handler is served by a real HTTP server on a free
loopback port, in front of a stub daemon.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from claude_code_ptt.http_api import make_handler


class FakeRegistry:
    def __init__(self):
        self.sessions = [{"pid": 11, "label": "one", "title": "One"},
                         {"pid": 22, "label": "two", "title": "Two"}]
        self.selected = 11

    def list(self):
        return list(self.sessions)

    def effective_pid(self):
        return self.selected


class FakeDaemon:
    def __init__(self):
        self.registry = FakeRegistry()
        self.paused = False
        self.selected_calls = []

    def ui_state(self):
        return {"phase": "idle", "highlight_pid": self.registry.selected,
                "speaking_pid": 0}

    def select_target(self, pid):
        self.selected_calls.append(pid)
        self.registry.selected = pid


@pytest.fixture
def api():
    daemon = FakeDaemon()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(daemon))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield daemon, base
    server.shutdown()
    server.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def _post(url, payload):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_ui_reports_phase_target_and_sessions(api):
    daemon, base = api
    daemon.paused = True
    state = _get(base + "/ui")
    assert state["phase"] == "idle"
    assert state["highlight_pid"] == 11
    assert state["speaking_pid"] == 0
    assert state["paused"] is True
    assert [s["pid"] for s in state["sessions"]] == [11, 22]


def test_target_selects_a_registered_session(api):
    daemon, base = api
    status, answer = _post(base + "/target", {"pid": 22})
    assert status == 200 and answer == {"ok": True, "target": 22}
    assert daemon.selected_calls == [22]
    assert _get(base + "/ui")["highlight_pid"] == 22


def test_target_refuses_an_unknown_session(api):
    daemon, base = api
    status, answer = _post(base + "/target", {"pid": 99})
    assert status == 404 and "error" in answer
    assert daemon.selected_calls == []


def test_target_without_pid_is_refused(api):
    daemon, base = api
    status, _answer = _post(base + "/target", {})
    assert status == 404
    assert daemon.selected_calls == []
