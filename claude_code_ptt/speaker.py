"""Spoken replies: edge-tts synthesis, played through Windows MCI (mp3-capable,
no extra audio dependencies)."""
import asyncio
import ctypes
import logging
import queue
import tempfile
import threading
import uuid
from pathlib import Path

winmm = ctypes.windll.winmm

log = logging.getLogger("claude_code_ptt")


def _mci(command: str) -> None:
    error = winmm.mciSendStringW(command, None, 0, None)
    if error:
        raise OSError(f"MCI error {error} for: {command}")


class Speaker:
    """Queued, interruptible text-to-speech playback.

    `hold_while` (optional callable) gates playback: as long as it returns
    True (e.g. while the mic is recording), queued speech waits instead of
    talking over the user. If it turns True mid-play, the audible reply is
    cut off but everything still queued survives and plays afterwards in
    order — only an explicit interrupt() drops the queue.

    The voice travels with each queued entry instead of being fixed here:
    every session speaks with its own (see voices.VoicePool), and a reply
    already queued keeps the voice of the session it came from.
    """

    def __init__(self, hold_while=None, *, rate: str = "+0%",
                 pitch: str = "+0Hz", volume: str = "+0%"):
        self.rate = rate
        self.pitch = pitch
        self.volume = volume
        self._hold_while = hold_while or (lambda: False)
        self._queue: queue.Queue[tuple[str, str, int]] = queue.Queue()
        self._interrupt = threading.Event()
        self._playing = threading.Event()
        self._origin = 0                   # session pid currently speaking
        self._purge_leftovers()
        threading.Thread(target=self._worker, daemon=True).start()

    @staticmethod
    def _purge_leftovers() -> None:
        """Synthesized files are deleted after playback, but a daemon killed
        mid-play leaves them behind - clean those up on the next start."""
        for path in Path(tempfile.gettempdir()).glob("ccptt-*.mp3"):
            try:
                path.unlink()
            except OSError:
                pass

    @property
    def playing(self) -> bool:
        return self._playing.is_set()

    @property
    def speaking_origin(self) -> int:
        """PID of the session whose text is playing right now (0 = none)."""
        return self._origin if self._playing.is_set() else 0

    def speak(self, text: str, voice: str, origin: int = 0) -> None:
        """Queue text for playback in a given voice; returns immediately."""
        self._queue.put((text, voice, origin))

    def interrupt(self) -> None:
        """Stop current playback and drop everything still queued."""
        self._interrupt.set()
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def _synthesize(self, text: str, voice: str) -> Path:
        import edge_tts
        path = Path(tempfile.gettempdir()) / f"ccptt-{uuid.uuid4().hex}.mp3"
        communicate = edge_tts.Communicate(
            text, voice, rate=self.rate, pitch=self.pitch,
            volume=self.volume,
        )
        asyncio.run(communicate.save(str(path)))
        return path

    def _play(self, path: Path) -> None:
        alias = f"ccptt_{uuid.uuid4().hex[:8]}"
        _mci(f'open "{path}" type mpegvideo alias {alias}')
        try:
            _mci(f"play {alias}")
            status = ctypes.create_unicode_buffer(32)
            while True:
                if self._interrupt.is_set() or self._hold_while():
                    break
                winmm.mciSendStringW(f"status {alias} mode", status, 32, None)
                if status.value != "playing":
                    break
                threading.Event().wait(0.1)
        finally:
            _mci(f"close {alias}")

    def _worker(self) -> None:
        while True:
            text, voice, origin = self._queue.get()
            self._interrupt.clear()
            self._origin = origin
            self._playing.set()
            path = None
            try:
                log.info("speaking %d chars as %s (session pid=%d)",
                         len(text), voice, origin)
                path = self._synthesize(text, voice)
                while self._hold_while() and not self._interrupt.is_set():
                    threading.Event().wait(0.1)
                if self._interrupt.is_set():
                    continue
                self._play(path)
            except Exception:              # noqa: BLE001
                log.exception("TTS failed")
            finally:
                self._playing.clear()
                if path is not None:
                    path.unlink(missing_ok=True)
