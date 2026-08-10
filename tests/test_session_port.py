"""Tests for finding the daemon of THIS Windows session.

With two accounts logged on, the other one's daemon answers on the same
loopback port while its overlay hangs on a desktop we cannot see. The
adapter has to recognise that, step aside to a free port, and leave a note
the daemon and both hooks of this session follow - and a machine with a
single account must behave exactly as it did before.

No daemon is ever started here: the adapter's spawn is recorded, not run.
"""
import socket
import urllib.error
import urllib.request
from types import SimpleNamespace

from claude_code_ptt import (confirm_hook, http_api, mcp_server, session_port,
                             turn_hook)
from claude_code_ptt.config import Config

OURS = 1
STRANGER = 2


def _notes_in(monkeypatch, tmp_path):
    """Keep the port note out of the real %APPDATA%."""
    monkeypatch.setattr(session_port, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(session_port, "session_id", lambda: OURS)


def _adapter(monkeypatch, answers: dict[int, dict]):
    """The adapter on a machine where `answers` says who replies on which
    port; ports not listed are silent. Spawning a daemon puts one of our
    own on the port the adapter settled for, as it would in reality.
    Returns the list the spawns are recorded in."""
    spawned: list[list[str]] = []
    monkeypatch.setattr(mcp_server, "_config", Config(daemon_port=8377))
    monkeypatch.setattr(mcp_server, "_windows_session", OURS)
    monkeypatch.setattr(mcp_server, "_port", 8377)
    monkeypatch.setattr(mcp_server.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(session_port, "in_use", lambda port: port in answers)

    def spawn(command, **kwargs):
        spawned.append(command)
        answers[mcp_server._port] = _status(OURS)

    def request(path, payload=None):
        if mcp_server._port not in answers:
            raise urllib.error.URLError("connection refused")
        return answers[mcp_server._port]

    monkeypatch.setattr(mcp_server.subprocess, "Popen", spawn)
    monkeypatch.setattr(mcp_server, "_request", request)
    return spawned


def _feed(monkeypatch, payload: bytes) -> None:
    """Hook input on stdin - both hooks read the same one, so it is set
    right before the hook that consumes it."""
    monkeypatch.setattr(confirm_hook.sys, "stdin", SimpleNamespace(
        buffer=SimpleNamespace(read=lambda: payload)))


def _status(session: int | None) -> dict:
    status = {"recording": False, "speaking": False, "target": 0,
              "version": "0.1.0"}
    if session is not None:
        status["session"] = session
    return status


def test_a_single_account_stays_on_the_configured_port(monkeypatch, tmp_path):
    _notes_in(monkeypatch, tmp_path)

    assert session_port.resolve(8377) == 8377
    assert list(tmp_path.iterdir()) == []   # nothing to note down


def test_the_noted_port_wins_over_the_configured_one(monkeypatch, tmp_path):
    _notes_in(monkeypatch, tmp_path)

    session_port.remember(8378)

    assert session_port.resolve(8377) == 8378


def test_every_windows_session_keeps_its_own_note(monkeypatch, tmp_path):
    """Two sessions of the SAME account share config.json - only the note
    per Windows session tells their daemons apart."""
    _notes_in(monkeypatch, tmp_path)
    session_port.remember(8378)
    monkeypatch.setattr(session_port, "session_id", lambda: STRANGER)

    assert session_port.resolve(8377) == 8377


def test_an_unreadable_note_falls_back_to_the_configured_port(
        monkeypatch, tmp_path):
    _notes_in(monkeypatch, tmp_path)
    session_port.note_file().write_text("not a port", encoding="utf-8")

    assert session_port.resolve(8377) == 8377


def test_a_free_port_is_searched_from_the_configured_one_upwards():
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        # asked repeatedly: the answer must not change with the listener's
        # backlog, or a busy daemon would look like a free port
        assert [session_port.in_use(port) for _ in range(3)] == [True] * 3

        chosen = session_port.free_port(port)

        assert chosen > port
        assert not session_port.in_use(chosen)

    assert not session_port.in_use(port)


def test_the_daemon_of_this_session_is_ours(monkeypatch):
    _adapter(monkeypatch, {8377: _status(OURS)})

    assert mcp_server._daemon_is_ours()


def test_the_daemon_of_another_windows_session_is_not_ours(monkeypatch):
    _adapter(monkeypatch, {8377: _status(STRANGER)})

    assert not mcp_server._daemon_is_ours()


def test_a_daemon_too_old_to_name_its_session_counts_as_ours(monkeypatch):
    """Upgrading the package does not restart a running daemon. Treating
    the one already there as foreign would start a second one, which loses
    the hotkey and dies - on a single-account machine it IS ours."""
    _adapter(monkeypatch, {8377: _status(None)})

    assert mcp_server._daemon_is_ours()


def test_something_that_is_not_our_daemon_is_not_ours(monkeypatch):
    _adapter(monkeypatch, {8377: {"error": "unknown path"}})

    assert not mcp_server._daemon_is_ours()


def test_a_running_daemon_of_our_own_is_left_alone(monkeypatch, tmp_path):
    _notes_in(monkeypatch, tmp_path)
    spawned = _adapter(monkeypatch, {8377: _status(OURS)})

    mcp_server._ensure_daemon()

    assert spawned == []
    assert mcp_server._port == 8377
    assert list(tmp_path.iterdir()) == []


def test_a_silent_port_gets_a_daemon_without_moving(monkeypatch, tmp_path):
    _notes_in(monkeypatch, tmp_path)
    spawned = _adapter(monkeypatch, {})

    mcp_server._ensure_daemon()

    assert len(spawned) == 1
    assert mcp_server._port == 8377
    assert list(tmp_path.iterdir()) == []


def test_a_foreign_daemon_pushes_the_adapter_to_a_free_port(
        monkeypatch, tmp_path):
    """The reproduced case: another logged-on account owns 8377. We must
    end up with our own daemon on a port of our own, noted down for the
    hooks of this session."""
    _notes_in(monkeypatch, tmp_path)
    spawned = _adapter(monkeypatch, {8377: _status(STRANGER)})

    mcp_server._ensure_daemon()

    assert len(spawned) == 1
    assert mcp_server._port == 8378
    assert session_port.resolve(8377) == 8378


def test_the_hooks_report_to_the_noted_port(monkeypatch, tmp_path):
    """The delivery proof travels through the hooks - if they keep posting
    to the configured port, the overlay goes silent instead of confirming."""
    _notes_in(monkeypatch, tmp_path)
    session_port.remember(8378)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cls()))
    posted: list[str] = []

    def urlopen(request, timeout=None):
        posted.append(request.full_url)
        return SimpleNamespace(read=lambda: b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(confirm_hook, "_note", lambda _message: None)

    _feed(monkeypatch, b'{"prompt": "[mic] hello"}')
    confirm_hook.main()
    _feed(monkeypatch, b'{"cwd": "C:/projects/one"}')
    turn_hook.main()

    assert posted == ["http://127.0.0.1:8378/prompt-received",
                      "http://127.0.0.1:8378/turn-state"]


def test_the_status_answer_names_the_windows_session_of_the_daemon():
    """What the adapter compares against - measured through the real
    endpoint, not through the handler in isolation."""
    daemon = SimpleNamespace(
        recorder=SimpleNamespace(recording=False),
        speaker=SimpleNamespace(playing=False),
        target_hwnd=lambda: 0,
    )
    port = session_port.free_port(8500)
    http_api.start(daemon, port)

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/status",
                                timeout=5) as answer:
        import json
        status = json.loads(answer.read())

    assert status["session"] == session_port.session_id()
