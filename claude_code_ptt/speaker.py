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
    talking over the user.
    """

    def __init__(self, voice: str, hold_while=None):
        self.voice = voice
        self._hold_while = hold_while or (lambda: False)
        self._queue: queue.Queue[tuple[str, int]] = queue.Queue()
        self._interrupt = threading.Event()
        self._playing = threading.Event()
        self._origin = 0                   # session pid currently speaking
        threading.Thread(target=self._worker, daemon=True).start()

    @property
    def playing(self) -> bool:
        return self._playing.is_set()

    @property
    def speaking_origin(self) -> int:
        """PID of the session whose text is playing right now (0 = none)."""
        return self._origin if self._playing.is_set() else 0

    def speak(self, text: str, origin: int = 0) -> None:
        """Queue text for playback; returns immediately."""
        self._queue.put((text, origin))

    def interrupt(self) -> None:
        """Stop current playback and drop everything still queued."""
        self._interrupt.set()
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def _synthesize(self, text: str) -> Path:
        import edge_tts
        path = Path(tempfile.gettempdir()) / f"ccptt-{uuid.uuid4().hex}.mp3"
        communicate = edge_tts.Communicate(text, self.voice)
        asyncio.run(communicate.save(str(path)))
        return path

    def _play(self, path: Path) -> None:
        alias = f"ccptt_{uuid.uuid4().hex[:8]}"
        _mci(f'open "{path}" type mpegvideo alias {alias}')
        try:
            _mci(f"play {alias}")
            status = ctypes.create_unicode_buffer(32)
            while True:
                if self._interrupt.is_set():
                    break
                winmm.mciSendStringW(f"status {alias} mode", status, 32, None)
                if status.value != "playing":
                    break
                threading.Event().wait(0.1)
        finally:
            _mci(f"close {alias}")

    def _worker(self) -> None:
        while True:
            text, origin = self._queue.get()
            self._interrupt.clear()
            self._origin = origin
            self._playing.set()
            path = None
            try:
                path = self._synthesize(text)
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
