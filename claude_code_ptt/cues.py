"""Audio cues: generated sine tones played through a dedicated output stream.

A near-silent warmup precedes every cue so speakers with auto-standby wake up
before the actual tone starts; short fades avoid clicks.
"""
import logging
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger("claude_code_ptt")

VOLUME = 0.5


def _build(cue_type: str, sr: int) -> np.ndarray | None:
    if cue_type == "record_start":
        t1 = np.linspace(0, 0.12, int(sr * 0.12), False)
        t2 = np.linspace(0, 0.12, int(sr * 0.12), False)
        tone1 = VOLUME * np.sin(2 * np.pi * 880 * t1)
        tone2 = VOLUME * np.sin(2 * np.pi * 1320 * t2)
        gap = np.zeros(int(sr * 0.04))
        return np.concatenate([tone1, gap, tone2])
    if cue_type == "record_stop":
        t = np.linspace(0, 0.12, int(sr * 0.12), False)
        return VOLUME * np.sin(2 * np.pi * 1100 * t)
    if cue_type == "error":
        t1 = np.linspace(0, 0.1, int(sr * 0.1), False)
        t2 = np.linspace(0, 0.12, int(sr * 0.12), False)
        tone1 = 0.45 * np.sin(2 * np.pi * 800 * t1)
        tone2 = 0.45 * np.sin(2 * np.pi * 500 * t2)
        gap = np.zeros(int(sr * 0.04))
        return np.concatenate([tone1, gap, tone2])
    return None


def _play(cue_type: str) -> None:
    try:
        sr = int(sd.query_devices(kind="output")["default_samplerate"])
        tone = _build(cue_type, sr)
        if tone is None:
            return
        fade = int(sr * 0.005)
        if len(tone) > fade * 2:
            tone[:fade] *= np.linspace(0, 1, fade)
            tone[-fade:] *= np.linspace(1, 0, fade)
        warmup = np.full(int(sr * 0.20), 0.001, dtype=np.float32)
        buf = np.concatenate([warmup, tone]).astype(np.float32)
        # Dedicated stream: robust against shared sd.play state and
        # concurrent TTS playback. Stereo = WASAPI shared mix format.
        stereo = np.column_stack([buf, buf])
        with sd.OutputStream(samplerate=sr, channels=2, dtype="float32") as st:
            st.write(stereo)
    except Exception:                      # noqa: BLE001
        log.exception("cue '%s' failed", cue_type)


def play_cue(cue_type: str) -> None:
    """Play a cue without blocking the caller."""
    threading.Thread(target=_play, args=(cue_type,), daemon=True).start()
