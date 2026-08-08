"""Microphone capture: 16 kHz mono float32, ready for Whisper."""
import threading

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16_000


class Recorder:
    """Start/stop recording from the default input device."""

    def __init__(self):
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()

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
