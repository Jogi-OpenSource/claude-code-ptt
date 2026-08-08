"""Local speech-to-text via faster-whisper."""
import numpy as np


class Transcriber:
    """Lazy-loads the Whisper model on first use (download can take a while)."""

    def __init__(self, model_name: str, language: str = ""):
        self._model_name = model_name
        self._language = language or None
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self._model_name, device="cpu",
                                       compute_type="int8")
        return self._model

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        segments, _info = self._ensure_model().transcribe(
            audio, language=self._language, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()
