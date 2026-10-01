"""Tests for pausing (POST /pause, Daemon.set_paused) and quitting
(Daemon.quit) the daemon.

While paused the hotkey must record nothing: the user keeps the microphone
open for another voice app, and nothing of that conversation may end up
injected into a session. Pausing mid-recording drops the recording, and
so does quitting.

No running daemon, no audio: the Daemon is built without __init__ and gets
stub recorder and mic objects.
"""
from types import SimpleNamespace

from claude_code_ptt import daemon as daemon_module
from claude_code_ptt.daemon import Daemon


class FakeRecorder:
    def __init__(self):
        self.recording = False
        self.starts = 0

    def start(self):
        self.recording = True
        self.starts += 1

    def stop(self):
        self.recording = False
        return SimpleNamespace(size=16_000)


class FakeMic:
    def __init__(self):
        self.restored = 0

    def open_for_recording(self):
        pass

    def restore(self):
        self.restored += 1


def make_daemon(monkeypatch) -> Daemon:
    monkeypatch.setattr(daemon_module, "play_cue", lambda *_: None)
    monkeypatch.setattr(daemon_module.events, "emit", lambda *_, **__: None)
    d = Daemon.__new__(Daemon)
    d.recorder = FakeRecorder()
    d.mic_mute = FakeMic()
    d.paused = False
    d._failed = False
    d._pending_text = None
    return d


def test_hotkey_records_nothing_while_paused(monkeypatch):
    d = make_daemon(monkeypatch)
    d.set_paused(True)
    d.toggle()
    assert not d.recorder.recording and d.recorder.starts == 0


def test_pausing_mid_recording_drops_it(monkeypatch):
    d = make_daemon(monkeypatch)
    d.toggle()
    assert d.recorder.recording
    d.set_paused(True)
    assert not d.recorder.recording and d.mic_mute.restored == 1


def test_resume_records_again(monkeypatch):
    d = make_daemon(monkeypatch)
    d.set_paused(True)
    d.set_paused(False)
    d.toggle()
    assert d.recorder.recording and d.recorder.starts == 1


def test_quit_ends_the_process(monkeypatch):
    d = make_daemon(monkeypatch)
    exits = []
    monkeypatch.setattr(daemon_module.os, "_exit", exits.append)
    d.quit()
    assert exits == [0]


def test_quitting_mid_recording_drops_it(monkeypatch):
    d = make_daemon(monkeypatch)
    monkeypatch.setattr(daemon_module.os, "_exit", lambda _code: None)
    d.toggle()
    d.quit()
    assert not d.recorder.recording and d.mic_mute.restored == 1
