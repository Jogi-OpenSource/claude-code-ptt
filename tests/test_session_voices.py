"""Tests for one edge-tts voice per session: the pool that hands them out
and keeps them, the registry that frees them, and the path from a
session's pid to the voice its reply is queued and played with.

No network anywhere - the edge-tts voice list is stubbed.
"""
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from claude_code_ptt import daemon, sessions, speaker, voices
from claude_code_ptt.config import Config

GERMAN = ["de-DE-AmalaNeural", "de-DE-ConradNeural", "de-DE-KatjaNeural"]


def _no_threads(monkeypatch) -> None:
    """Registry reaper and daemon watchdogs must not outlive the test."""
    monkeypatch.setattr(
        sessions.threading, "Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: None),
    )


def _stub_catalogue(monkeypatch, catalogue=GERMAN) -> None:
    """Voice list without a network. The list is read on every lookup, so
    a test can let it arrive late."""
    monkeypatch.setattr(voices, "catalogue", lambda locale: list(catalogue))


def _pool(monkeypatch, default: str, catalogue=GERMAN) -> voices.VoicePool:
    _stub_catalogue(monkeypatch, catalogue)
    pool = voices.VoicePool(default)
    pool._load()                           # what prefetch does at startup
    return pool


def _registry(monkeypatch, pool) -> sessions.SessionRegistry:
    _no_threads(monkeypatch)
    monkeypatch.setattr(sessions, "find_session_window", lambda pid: 0)
    return sessions.SessionRegistry(pool)


def _running(monkeypatch, *pids: int) -> None:
    """Only these pids still exist as processes."""
    monkeypatch.setattr(sessions, "process_tree",
                        lambda: {pid: (0, "claude.exe") for pid in pids})


def _daemon(monkeypatch) -> tuple[daemon.Daemon, list]:
    """A daemon with everything hardware-bound stubbed out, plus the list
    of (text, voice, pid) it would have spoken."""
    queued: list[tuple[str, str, int]] = []

    class FakeSpeaker:
        def __init__(self, **kwargs):
            pass

        def speak(self, text, voice, origin=0):
            queued.append((text, voice, origin))

    monkeypatch.setattr(daemon, "Recorder",
                        lambda **kwargs: SimpleNamespace(recording=False))
    monkeypatch.setattr(daemon, "MicMute", lambda: object())
    monkeypatch.setattr(daemon, "Transcriber", lambda *args: object())
    monkeypatch.setattr(daemon, "Speaker", FakeSpeaker)
    monkeypatch.setattr(daemon, "Overlay", lambda owner: None)
    _stub_catalogue(monkeypatch)
    _no_threads(monkeypatch)
    monkeypatch.setattr(sessions, "find_session_window", lambda pid: 0)
    instance = daemon.Daemon(Config(tts_voice="de-DE-ConradNeural"))
    instance.voices._load()                # the prefetch, without its thread
    return instance, queued


def test_locale_of_a_voice_name():
    assert voices.locale_of("de-DE-KatjaNeural") == "de-DE"
    assert voices.locale_of("en-US-GuyNeural") == "en-US"
    assert voices.locale_of("nonsense") == ""


def test_locale_of_a_voice_whose_locale_has_three_parts():
    """edge-tts offers these for real - cutting them down to two parts
    would look up a locale that does not exist and leave the session
    alone with the configured voice."""
    assert voices.locale_of("iu-Latn-CA-SiqiniqNeural") == "iu-Latn-CA"
    assert voices.locale_of("zh-CN-liaoning-XiaobeiNeural") == "zh-CN-liaoning"


def test_catalogue_lists_only_the_asked_for_locale(monkeypatch):
    async def list_voices():
        return [
            {"ShortName": "de-DE-KatjaNeural", "Locale": "de-DE"},
            {"ShortName": "de-DE-AmalaNeural", "Locale": "de-DE"},
            {"ShortName": "en-US-GuyNeural", "Locale": "en-US"},
        ]

    monkeypatch.setitem(sys.modules, "edge_tts",
                        SimpleNamespace(list_voices=list_voices))

    assert voices.catalogue("de-DE") == ["de-DE-AmalaNeural",
                                         "de-DE-KatjaNeural"]


def test_catalogue_survives_an_unreachable_voice_list(monkeypatch):
    async def list_voices():
        raise OSError("no network")

    monkeypatch.setitem(sys.modules, "edge_tts",
                        SimpleNamespace(list_voices=list_voices))

    assert voices.catalogue("de-DE") == []


def test_the_first_session_speaks_with_the_configured_voice(monkeypatch):
    pool = _pool(monkeypatch, "de-DE-ConradNeural")

    assert pool.voice_for(101) == "de-DE-ConradNeural"


def test_further_sessions_get_the_other_voices_of_the_locale(monkeypatch):
    pool = _pool(monkeypatch, "de-DE-ConradNeural")

    handed = [pool.voice_for(pid) for pid in (101, 202, 303)]

    assert handed == ["de-DE-ConradNeural", "de-DE-AmalaNeural",
                      "de-DE-KatjaNeural"]
    assert pool.voice_for(404) in handed   # more sessions than voices


def test_a_session_asking_again_gets_the_same_voice(monkeypatch):
    pool = _pool(monkeypatch, "de-DE-ConradNeural")
    first = pool.voice_for(101)
    pool.voice_for(202)

    assert pool.voice_for(101) == first


def test_a_closed_session_hands_its_voice_back(monkeypatch):
    pool = _pool(monkeypatch, "de-DE-ConradNeural")
    for pid in (101, 202, 303):
        pool.voice_for(pid)

    pool.retain({101, 303})                # pid 202 is gone

    assert pool.voice_for(404) == "de-DE-AmalaNeural"


def test_an_unreadable_process_list_keeps_every_voice(monkeypatch):
    """A failed snapshot is not proof that every session died - taking it
    at face value would reshuffle all voices at once."""
    pool = _pool(monkeypatch, "de-DE-ConradNeural")
    handed = {pid: pool.voice_for(pid) for pid in (101, 202)}

    pool.retain(set())

    assert {pid: pool.voice_for(pid) for pid in (101, 202)} == handed


def test_sessions_asking_at_the_same_moment_differ(monkeypatch):
    """Requests are served on the HTTP server's thread pool: two sessions
    can ask for a voice at the very same moment and must not end up
    sharing one."""
    pool = _pool(monkeypatch, "de-DE-ConradNeural")
    pids = (101, 202, 303)
    together = threading.Barrier(len(pids))
    handed: dict[int, str] = {}

    def ask(pid):
        together.wait()
        handed[pid] = pool.voice_for(pid)

    askers = [threading.Thread(target=ask, args=(pid,)) for pid in pids]
    for asker in askers:
        asker.start()
    for asker in askers:
        asker.join()

    assert sorted(handed.values()) == GERMAN


def test_a_registration_never_waits_for_the_voice_list(monkeypatch):
    """The MCP adapter gives the daemon ten seconds per request and a
    registration already spends up to five of them probing for the
    session's window - a voice lookup on top of that timed sessions out.
    The list is therefore fetched in the background and callers take
    whatever has arrived."""
    hangs = threading.Event()

    def hanging_catalogue(_locale):
        hangs.wait(10)
        return []

    monkeypatch.setattr(voices, "catalogue", hanging_catalogue)
    pool = voices.VoicePool("de-DE-ConradNeural")
    pool.prefetch()
    registry = _registry(monkeypatch, pool)
    started = time.monotonic()
    try:
        registry.register(101, r"C:\projects\one")

        assert pool.voice_for(101) == "de-DE-ConradNeural"
        assert time.monotonic() - started < 1
    finally:
        hangs.set()


def test_the_rotation_starts_once_the_voice_list_is_there(monkeypatch):
    """Sessions speaking before it arrives all use the configured voice,
    and none of them is pinned to it. A failed lookup is not cached
    either, so the daemon does not stay on one voice for life."""
    arriving: list[str] = []
    pool = _pool(monkeypatch, "de-DE-ConradNeural", catalogue=arriving)

    assert [pool.voice_for(pid) for pid in (101, 202)] == \
        ["de-DE-ConradNeural"] * 2

    arriving.extend(GERMAN)
    pool._load()                                # the retry they kicked off

    assert [pool.voice_for(pid) for pid in (101, 202)] == \
        ["de-DE-ConradNeural", "de-DE-AmalaNeural"]


def test_without_a_voice_list_everyone_uses_the_configured_voice(monkeypatch):
    pool = _pool(monkeypatch, "de-DE-ConradNeural", catalogue=[])

    assert pool.voice_for(101) == "de-DE-ConradNeural"
    assert pool.voice_for(202) == "de-DE-ConradNeural"


def test_a_locale_with_a_single_voice_is_looked_up_once(monkeypatch):
    """A list holding nothing but the configured voice is a complete
    answer, not a failed lookup to be retried on every reply."""
    lookups: list[str] = []

    def counted_catalogue(locale):
        lookups.append(locale)
        return ["de-DE-ConradNeural"]

    monkeypatch.setattr(voices, "catalogue", counted_catalogue)
    pool = voices.VoicePool("de-DE-ConradNeural")
    pool._load()

    assert [pool.voice_for(pid) for pid in (101, 202)] == \
        ["de-DE-ConradNeural"] * 2
    assert lookups == ["de-DE"]


def test_a_session_keeps_its_voice_when_its_registration_expires(monkeypatch):
    """Missed heartbeats expire a registration while the session itself
    keeps running - it re-registers and must sound the same as before."""
    pool = _pool(monkeypatch, "de-DE-ConradNeural")
    registry = _registry(monkeypatch, pool)
    registry.register(101, r"C:\projects\one")
    registry.register(202, r"C:\projects\two")
    handed = {pid: pool.voice_for(pid) for pid in (101, 202)}
    monkeypatch.setattr(sessions, "HEARTBEAT_TIMEOUT", -1)
    _running(monkeypatch, 101, 202)

    registry._reap()

    assert registry.list() == []                # no longer registered
    assert {pid: pool.voice_for(pid) for pid in (101, 202)} == handed


def test_a_goodbye_keeps_the_voice_of_a_session_that_lives_on(monkeypatch):
    """A restarted MCP adapter says goodbye for a session that keeps
    running - the voice must still be waiting when it registers again,
    and must not have been handed to somebody else meanwhile."""
    pool = _pool(monkeypatch, "de-DE-ConradNeural")
    registry = _registry(monkeypatch, pool)
    registry.register(101, r"C:\projects\one")
    first = pool.voice_for(101)

    registry.unregister(101)
    other = pool.voice_for(202)

    assert pool.voice_for(101) == first
    assert other != first


def test_a_session_whose_process_is_gone_frees_its_voice(monkeypatch):
    """Two voices, so the next session can only get one of them back."""
    pool = _pool(monkeypatch, "de-DE-ConradNeural", catalogue=GERMAN[:2])
    registry = _registry(monkeypatch, pool)
    registry.register(101, r"C:\projects\one")
    registry.register(202, r"C:\projects\two")
    pool.voice_for(101)
    second = pool.voice_for(202)
    monkeypatch.setattr(sessions, "HEARTBEAT_TIMEOUT", -1)
    _running(monkeypatch, 101)                  # pid 202 is gone

    registry._reap()

    assert pool.voice_for(303) == second


def test_the_daemon_speaks_in_the_voice_of_the_session(monkeypatch):
    instance, queued = _daemon(monkeypatch)
    instance.registry.register(101, r"C:\projects\one")
    instance.registry.register(202, r"C:\projects\two")

    instance.speak("first answer", 101)
    instance.speak("other session", 202)
    instance.speak("second answer", 101)
    instance.speak("nobody in particular")

    assert queued[0] == ("first answer", "de-DE-ConradNeural", 101)
    assert queued[2] == ("second answer", "de-DE-ConradNeural", 101)
    assert queued[1] == ("other session", "de-DE-AmalaNeural", 202)
    assert queued[3] == ("nobody in particular", "de-DE-ConradNeural", 0)


def test_the_daemon_speaks_on_while_a_registration_is_missing(monkeypatch):
    """Between an expired registration and the next heartbeat a session
    is unknown to the registry but very much alive and still speaking -
    it must not drop to the configured voice for those seconds."""
    instance, queued = _daemon(monkeypatch)
    instance.registry.register(101, r"C:\projects\one")
    instance.registry.register(202, r"C:\projects\two")
    instance.speak("before", 101)
    instance.speak("before", 202)
    monkeypatch.setattr(sessions, "HEARTBEAT_TIMEOUT", -1)
    _running(monkeypatch, 101, 202)

    instance.registry._reap()
    instance.speak("after", 202)

    assert queued[2][1] == queued[1][1]
    assert queued[2][1] != instance.voices.default


def test_replies_waiting_out_a_recording_keep_their_voices(monkeypatch):
    """A recording holds playback back instead of dropping the queue (see
    43a0246) - and every waiting reply comes out in the voice of the
    session it came from, not in the one that happens to speak last."""
    # the live daemon's files are none of this test's business
    monkeypatch.setattr(speaker.Speaker, "_purge_leftovers",
                        staticmethod(lambda: None))
    recording = threading.Event()
    played: list[str] = []
    both_played = threading.Event()

    def synthesize(text, voice):
        return Path(tempfile.gettempdir()) / f"ccptt-{text}-{voice}.mp3"

    def play(path):
        played.append(path.name)
        if len(played) == 2:
            both_played.set()

    talker = speaker.Speaker(hold_while=recording.is_set)
    monkeypatch.setattr(talker, "_synthesize", synthesize)
    monkeypatch.setattr(talker, "_play", play)

    recording.set()
    talker.speak("first", "de-DE-ConradNeural", 101)
    talker.speak("second", "de-DE-AmalaNeural", 202)
    assert not both_played.wait(0.3)           # held back, not dropped
    recording.clear()

    assert both_played.wait(5)
    assert played == ["ccptt-first-de-DE-ConradNeural.mp3",
                      "ccptt-second-de-DE-AmalaNeural.mp3"]
