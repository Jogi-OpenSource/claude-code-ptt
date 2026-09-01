"""Microphone capture: 16 kHz mono float32, ready for Whisper."""
import threading

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16_000


class Recorder:
    """Start/stop recording from the default input device.

    `on_level` (optional) is called with the loudness of each captured block
    (0.0-1.0) - enough for a level meter. It runs on the audio callback
    thread, so it must return immediately and must never raise; a listener
    that throws would otherwise kill the capture stream mid-recording.
    """

    def __init__(self, on_level=None):
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._on_level = on_level

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            self._chunks = []
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=self._on_audio,
            )
            self._stream.start()

    def _on_audio(self, indata, _frames, _time, _status) -> None:
        self._chunks.append(indata.copy())
        if self._on_level is not None:
            try:
                self._on_level(float(np.abs(indata).max()))
            except Exception:              # noqa: BLE001
                pass                       # a listener must never stop capture

    def stop(self) -> np.ndarray:
        """Stop and return the recording as a mono float32 array."""
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return np.empty(0, dtype=np.float32)
        stream.stop()
        stream.close()
        if not self._chunks:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(self._chunks).flatten()
