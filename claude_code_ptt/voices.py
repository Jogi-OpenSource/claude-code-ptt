"""One edge-tts voice per Claude Code session.

With six terminals talking through the same daemon every reply sounded
alike - you could not hear which session was speaking. Voices are handed
out round-robin from the ones sharing the configured voice's locale and
stay with a session for as long as its process lives.

Which voices exist is Microsoft's answer over the network, so it is asked
for once in the background: nothing here ever waits on it. A session
registration least of all - the MCP adapter gives the daemon ten seconds
per request, and probing for the session's window already spends most of
them.
"""
import asyncio
import logging
import threading

log = logging.getLogger("claude_code_ptt")

LOOKUP_TIMEOUT = 5.0


def locale_of(voice: str) -> str:
    """Locale part of an edge-tts voice name ('de-DE-KatjaNeural' ->
    'de-DE'); '' if the name does not look like one.

    Only the last dash-separated part is the voice itself - not every
    locale has two parts ('iu-Latn-CA-SiqiniqNeural' -> 'iu-Latn-CA',
    'zh-CN-liaoning-XiaobeiNeural' -> 'zh-CN-liaoning')."""
    locale, _, name = voice.rpartition("-")
    return locale if locale and name else ""


def catalogue(locale: str) -> list[str]:
    """Every edge-tts voice of a locale, alphabetically; empty if the list
    cannot be fetched."""
    if not locale:
        return []
    try:
        import edge_tts

        async def fetch():
            return await asyncio.wait_for(edge_tts.list_voices(),
                                          LOOKUP_TIMEOUT)

        entries = asyncio.run(fetch())
    except Exception:                      # noqa: BLE001
        log.exception("could not list edge-tts voices")
        return []
    return sorted(str(entry.get("ShortName") or "") for entry in entries
                  if entry.get("Locale") == locale and entry.get("ShortName"))


class VoicePool:
    """Hands out a voice per session pid, the configured one first.

    A single session therefore sounds exactly as it always did; further
    sessions take the remaining voices of the same locale in turn, and the
    voice of a session whose process is gone is handed out again.

    The pool owns the pid -> voice assignment rather than the session
    registry, for two reasons: handing out and remembering a voice happen
    under one lock, so sessions asking at the same moment cannot end up
    sharing one; and a voice has to outlive its registration. A
    registration ends for reasons that have nothing to do with the session
    ending - missed heartbeats expire it, and a restarted adapter says
    goodbye on behalf of a session that keeps running - and in both cases
    it re-registers moments later and has to keep sounding the same. Only
    a process that is really gone (`retain`) puts a voice back into
    rotation.
    """

    def __init__(self, default_voice: str):
        self.default = default_voice
        self._lock = threading.Lock()
        self._voices: list[str] | None = None
        self._loading = False
        self._next = 0
        self._assigned: dict[int, str] = {}     # session pid -> voice

    def prefetch(self) -> None:
        """Fetch the locale's voices in the background, once.

        Cheap to call again: a failed fetch is not cached, so the next
        session asking for a voice retries it - otherwise a daemon that
        started without a network would sound the same for the rest of its
        life."""
        with self._lock:
            if self._voices is not None or self._loading:
                return
            self._loading = True
        threading.Thread(target=self._load, daemon=True).start()

    def voice_for(self, pid: int) -> str:
        """The voice of a session: assigned on first sight, kept afterwards.

        Returns at once, always. Until the voice list has arrived every
        session speaks with the configured voice and none of them is
        pinned to it - the rotation starts with the list, and the voice it
        hands out first is the configured one anyway."""
        with self._lock:
            known = self._assigned.get(pid)
            if known:
                return known
            if self._voices is not None:
                self._assigned[pid] = self._take()
                return self._assigned[pid]
        self.prefetch()
        return self.default

    def retain(self, alive: set[int]) -> None:
        """Keep only the voices of processes that still exist.

        An empty snapshot means the process enumeration failed, not that
        every session died at once - honouring it would reshuffle every
        voice, so it is ignored."""
        if not alive:
            return
        with self._lock:
            for pid in [pid for pid in self._assigned if pid not in alive]:
                del self._assigned[pid]

    def _load(self) -> None:
        """Body of the prefetch thread."""
        found = catalogue(locale_of(self.default))
        with self._lock:
            if found:
                self._voices = [self.default] + [voice for voice in found
                                                 if voice != self.default]
                log.info("%d voices for %s", len(self._voices), self.default)
            self._loading = False

    def _take(self) -> str:
        """The next voice no session is using. Call with the lock held."""
        voices = self._voices
        taken = set(self._assigned.values())
        for offset in range(len(voices)):
            voice = voices[(self._next + offset) % len(voices)]
            if voice not in taken:
                self._next = (self._next + offset + 1) % len(voices)
                return voice
        # more sessions than voices: they have to share, but the rotation
        # continues so the doubling spreads out
        voice = voices[self._next]
        self._next = (self._next + 1) % len(voices)
        return voice
