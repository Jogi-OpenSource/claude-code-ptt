"""Tests for finding the daemon of THIS Windows session.

With two accounts logged on, the other one's daemon answers on the same
loopback port while its overlay hangs on a desktop we cannot see. The
adapter has to recognise that and start one of its own; that daemon has to
get a port even though the stranger's daemon chooses at the same moment,
and has to leave a note the adapter and both hooks of this session follow -
and a machine with a single account must behave exactly as it did before.

No daemon is ever started here: the adapter's spawn is recorded, not run.
"""
import json
import os
import socket
import threading
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


def _adapter(monkeypatch, tmp_path, answers: dict[int, dict]):
    """The adapter on a machine where `answers` says who replies on which
    port; ports not listed are silent. A spawned daemon does what the real
    one does: it binds the first port nobody else answers on and notes it
    down. Returns the list the spawns are recorded in."""
    spawned: list[list[str]] = []
    _notes_in(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_server, "_config", Config(daemon_port=8377))
    monkeypatch.setattr(mcp_server, "_windows_session", OURS)
    monkeypatch.setattr(mcp_server, "_port", 8377)
    monkeypatch.setattr(mcp_server.time, "sleep", lambda _seconds: None)

    def spawn(command, **kwargs):
        spawned.append(command)
        port = next(p for p in range(8377, 8377 + http_api.PORT_SCAN)
                    if p not in answers)
        answers[port] = _status(OURS)
        session_port.announce(port, 8377)

    def request(path, payload=None):
        if mcp_server._port not in answers:
            raise urllib.error.URLError("connection refused")
        return answers[mcp_server._port]

    monkeypatch.setattr(mcp_server.subprocess, "Popen", spawn)
    monkeypatch.setattr(mcp_server, "_request", request)
    return spawned


def _recorded(monkeypatch, answers: dict[int, dict],
              known: bool = True) -> list[tuple[str, int]]:
    """Record every request with the port it actually went to - what the
    adapter believes about the port is the whole point here. `known` is what
    the daemon says about our registration."""
    asked: list[tuple[str, int]] = []

    def request(path, payload=None):
        asked.append((path, mcp_server._port))
        if mcp_server._port not in answers:
            raise urllib.error.URLError("connection refused")
        if path == "/status":
            return answers[mcp_server._port]
        return {"ok": known}

    monkeypatch.setattr(mcp_server, "_request", request)
    return asked


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


def _daemon_stub():
    """Just enough daemon for the /status endpoint to answer."""
    return SimpleNamespace(
        recorder=SimpleNamespace(recording=False),
        speaker=SimpleNamespace(playing=False),
        target_hwnd=lambda: 0,
    )


def _free_port() -> int:
    """A port nobody owns at this moment - where a daemon under test starts
    looking."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _status_of(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/status",
                                timeout=5) as answer:
        return json.loads(answer.read())


def test_a_single_account_stays_on_the_configured_port(monkeypatch, tmp_path):
    _notes_in(monkeypatch, tmp_path)

    assert session_port.resolve(8377) == 8377
    assert list(tmp_path.iterdir()) == []   # nothing to note down


def test_the_noted_port_wins_over_the_configured_one(monkeypatch, tmp_path):
    _notes_in(monkeypatch, tmp_path)

    session_port.announce(8378, 8377)

    assert session_port.resolve(8377) == 8378


def test_every_windows_session_keeps_its_own_note(monkeypatch, tmp_path):
    """Two sessions of the SAME account share config.json - only the note
    per Windows session tells their daemons apart."""
    _notes_in(monkeypatch, tmp_path)
    session_port.announce(8378, 8377)
    monkeypatch.setattr(session_port, "session_id", lambda: STRANGER)

    assert session_port.resolve(8377) == 8377


def test_an_unreadable_note_falls_back_to_the_configured_port(
        monkeypatch, tmp_path):
    _notes_in(monkeypatch, tmp_path)
    session_port.note_file().write_text("not a port", encoding="utf-8")

    assert session_port.resolve(8377) == 8377


def test_the_note_goes_when_the_configured_port_is_free_again(
        monkeypatch, tmp_path):
    """The other account logged off: the next daemon takes the default
    port back, and a note still pointing elsewhere would mute the hooks."""
    _notes_in(monkeypatch, tmp_path)
    session_port.announce(8378, 8377)

    session_port.announce(8377, 8377)

    assert session_port.resolve(8377) == 8377
    assert list(tmp_path.iterdir()) == []


def test_two_daemons_starting_at_once_get_a_port_each():
    """The reproduced case from the daemons' side: two logged-on accounts
    start theirs in the same moment. A free-port lookup would hand both the
    same answer and one of them would end up without a daemon - only the
    bind decides, and whoever loses it moves up."""
    base = _free_port()
    both_there = threading.Barrier(2)
    ports: list[int] = []

    def start():
        both_there.wait()
        ports.append(http_api.start(_daemon_stub(), base))

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(ports)) == 2             # neither was left without one
    assert min(ports) == base               # and nobody drifted off for fun
    for port in ports:
        assert _status_of(port)["session"] == session_port.session_id()


def test_only_one_daemon_per_windows_session_claims_the_instance(monkeypatch):
    """Every adapter that finds no daemon spawns one; all but the first
    must step back before they take a microphone, an overlay or a port."""
    monkeypatch.setattr(session_port, "INSTANCE_MUTEX",
                        f"Local\\claude-code-ptt-test-{os.getpid()}")

    assert session_port.claim_instance()
    assert not session_port.claim_instance()


def test_the_daemon_of_this_session_is_ours(monkeypatch, tmp_path):
    _adapter(monkeypatch, tmp_path, {8377: _status(OURS)})

    assert mcp_server._found_our_daemon()


def test_the_daemon_of_another_windows_session_is_not_ours(
        monkeypatch, tmp_path):
    _adapter(monkeypatch, tmp_path, {8377: _status(STRANGER)})

    assert not mcp_server._found_our_daemon()


def test_a_daemon_too_old_to_name_its_session_counts_as_ours(
        monkeypatch, tmp_path):
    """Upgrading the package does not restart a running daemon. Treating
    the one already there as foreign would start a second one, which loses
    the hotkey and dies - on a single-account machine it IS ours."""
    _adapter(monkeypatch, tmp_path, {8377: _status(None)})

    assert mcp_server._found_our_daemon()


def test_something_that_is_not_our_daemon_is_not_ours(monkeypatch, tmp_path):
    _adapter(monkeypatch, tmp_path, {8377: {"error": "unknown path"}})

    assert not mcp_server._found_our_daemon()


def test_a_running_daemon_of_our_own_is_left_alone(monkeypatch, tmp_path):
    spawned = _adapter(monkeypatch, tmp_path, {8377: _status(OURS)})

    mcp_server._ensure_daemon()

    assert spawned == []
    assert mcp_server._port == 8377
    assert list(tmp_path.iterdir()) == []


def test_a_silent_port_gets_a_daemon_without_moving(monkeypatch, tmp_path):
    spawned = _adapter(monkeypatch, tmp_path, {})

    mcp_server._ensure_daemon()

    assert len(spawned) == 1
    assert mcp_server._port == 8377
    assert list(tmp_path.iterdir()) == []


def test_a_foreign_daemon_pushes_the_adapter_to_a_free_port(
        monkeypatch, tmp_path):
    """The reproduced case: another logged-on account owns 8377. We must
    end up with our own daemon on a port of our own, noted down for the
    hooks of this session."""
    spawned = _adapter(monkeypatch, tmp_path, {8377: _status(STRANGER)})

    mcp_server._ensure_daemon()

    assert len(spawned) == 1
    assert mcp_server._port == 8378
    assert session_port.resolve(8377) == 8378


def test_a_later_adapter_joins_the_daemon_instead_of_adding_one(
        monkeypatch, tmp_path):
    """Every adapter resolves its port once, at import time. The daemon
    that stepped aside notes the new port down - a sibling adapter still
    holding the stranger's port must pick that up, or it spawns daemon
    after daemon."""
    spawned = _adapter(monkeypatch, tmp_path, {8377: _status(STRANGER)})
    mcp_server._ensure_daemon()            # daemon lands on 8378

    monkeypatch.setattr(mcp_server, "_port", 8377)   # sibling, still stale
    mcp_server._ensure_daemon()

    assert len(spawned) == 1
    assert mcp_server._port == 8378
    assert session_port.resolve(8377) == 8378


def test_the_adapter_waits_for_a_daemon_that_is_still_coming_up(
        monkeypatch, tmp_path):
    """Loading Whisper takes a daemon seconds; an adapter that gave up
    after the spawn would report a failure and spawn again next time."""
    _adapter(monkeypatch, tmp_path, {})
    asked = []

    def request(path, payload=None):
        asked.append(path)
        if len(asked) < 4:                 # the daemon is still coming up
            raise urllib.error.URLError("connection refused")
        return _status(OURS)

    monkeypatch.setattr(mcp_server, "_request", request)

    mcp_server._ensure_daemon()

    assert len(asked) == 4                 # one look before, three after


def test_the_heartbeat_follows_the_daemon_back_to_the_configured_port(
        monkeypatch, tmp_path):
    """The stranger logged off, so our daemon restarted on the default port
    and took its note back. An adapter that keeps heartbeating where the
    daemon stood falls out of the overlay and stays out until its next tool
    call - the round has to resolve the port again, not reuse it."""
    _adapter(monkeypatch, tmp_path, {8377: _status(OURS)})
    monkeypatch.setattr(mcp_server, "_port", 8378)   # where the daemon was
    asked = _recorded(monkeypatch, {8377: _status(OURS)})

    mcp_server._heartbeat({"pid": 4711, "cwd": "C:/projects/one"})

    assert asked == [("/status", 8377), ("/heartbeat", 8377)]


def test_the_heartbeat_does_not_join_a_foreign_daemon(monkeypatch, tmp_path):
    """Ours is gone and the other account's daemon answers where it stood.
    Registering there would list this session in an overlay on a desktop
    nobody here can see."""
    _adapter(monkeypatch, tmp_path, {})
    asked = _recorded(monkeypatch, {8377: _status(STRANGER)})

    mcp_server._heartbeat({"pid": 4711, "cwd": "C:/projects/one"})

    assert asked == [("/status", 8377)]     # asked, and left it alone


def test_a_restarted_daemon_gets_this_session_back(monkeypatch, tmp_path):
    """A daemon that came up again knows nobody - the heartbeat coming back
    "unknown" is what puts this session into its overlay again."""
    _adapter(monkeypatch, tmp_path, {8377: _status(OURS)})
    asked = _recorded(monkeypatch, {8377: _status(OURS)}, known=False)

    mcp_server._heartbeat({"pid": 4711, "cwd": "C:/projects/one"})

    assert [path for path, _port in asked] == ["/status", "/heartbeat",
                                               "/register"]


def test_the_hooks_report_to_the_noted_port(monkeypatch, tmp_path):
    """The delivery proof travels through the hooks - if they keep posting
    to the configured port, the overlay goes silent instead of confirming."""
    _notes_in(monkeypatch, tmp_path)
    session_port.announce(8378, 8377)
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
    port = http_api.start(_daemon_stub(), _free_port())

    assert _status_of(port)["session"] == session_port.session_id()
