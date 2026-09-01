"""Tests for edge-tts voice controls from config through synthesis."""
import sys
from types import SimpleNamespace

from claude_code_ptt.config import Config
from claude_code_ptt import daemon, speaker


def test_config_uses_neutral_edge_tts_voice_controls():
    config = Config()

    assert config.tts_rate == "+0%"
    assert config.tts_pitch == "+0Hz"
    assert config.tts_volume == "+0%"


def test_speaker_keeps_positional_hold_callback(monkeypatch):
    hold_while = lambda: True
    monkeypatch.setattr(speaker.Speaker, "_purge_leftovers", lambda self: None)
    monkeypatch.setattr(
        speaker.threading, "Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: None),
    )

    instance = speaker.Speaker(hold_while)

    assert instance._hold_while is hold_while
    assert instance.rate == "+0%"
    assert instance.pitch == "+0Hz"
    assert instance.volume == "+0%"


def test_speaker_passes_voice_controls_to_edge_tts(monkeypatch, tmp_path):
    received = {}

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            received.update(text=text, voice=voice, **kwargs)

        async def save(self, path):
            received["path"] = path

    monkeypatch.setitem(
        sys.modules, "edge_tts", SimpleNamespace(Communicate=FakeCommunicate)
    )
    monkeypatch.setattr(speaker.tempfile, "gettempdir", lambda: str(tmp_path))
    instance = speaker.Speaker.__new__(speaker.Speaker)
    instance.rate = "+35%"
    instance.pitch = "-20Hz"
    instance.volume = "+10%"

    path = instance._synthesize("The same test phrase",
                                "de-DE-FlorianMultilingualNeural")

    assert received == {
        "text": "The same test phrase",
        "voice": "de-DE-FlorianMultilingualNeural",
        "rate": "+35%",
        "pitch": "-20Hz",
        "volume": "+10%",
        "path": str(path),
    }


def test_daemon_passes_configured_voice_controls_to_speaker(monkeypatch):
    received = {}

    class FakeSpeaker:
        def __init__(self, rate=None, pitch=None, volume=None,
                     hold_while=None, on_play=None):
            received.update(
                rate=rate,
                pitch=pitch,
                volume=volume,
                hold_while=hold_while,
            )

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    recorder = SimpleNamespace(recording=False)
    monkeypatch.setattr(daemon, "Recorder", lambda **kwargs: recorder)
    monkeypatch.setattr(daemon, "MicMute", lambda: object())
    monkeypatch.setattr(daemon, "Transcriber", lambda *args: object())
    monkeypatch.setattr(daemon, "Speaker", FakeSpeaker)
    monkeypatch.setattr(daemon, "SessionRegistry", lambda voices: None)
    monkeypatch.setattr(daemon, "Overlay", lambda owner: None)
    monkeypatch.setattr(daemon.threading, "Thread", FakeThread)
    config = Config(
        tts_voice="de-DE-FlorianMultilingualNeural",
        tts_rate="+35%",
        tts_pitch="-20Hz",
        tts_volume="+10%",
    )

    daemon.Daemon(config)

    hold_while = received.pop("hold_while")
    assert received == {
        "rate": "+35%",
        "pitch": "-20Hz",
        "volume": "+10%",
    }
    assert hold_while() is False
