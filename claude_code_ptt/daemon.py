"""The claude-code-ptt daemon: hotkey -> record -> transcribe -> inject.

Single instance per machine. Owns the global hotkey (default Ctrl+M) and the
microphone; the MCP adapter talks to it over localhost HTTP (Phase 2).
"""
import ctypes
import ctypes.wintypes
import logging
import sys
import threading
import time

from . import http_api
from .config import Config, config_dir
from .cues import play_cue
from .injector import TargetTracker, inject_text
from .overlay import Overlay
from .recorder import Recorder
from .sessions import SessionRegistry
from .speaker import Speaker
from .transcriber import Transcriber

user32 = ctypes.windll.user32

MOD_FLAGS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
HOTKEY_ID = 1
MIC_PREFIX = "\U0001F3A4 "                 # microphone emoji

log = logging.getLogger("claude_code_ptt")


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        self.recorder = Recorder()
        self.transcriber = Transcriber(config.whisper_model, config.language)
        self.tracker = TargetTracker(config.window_title_markers)
        self.speaker = Speaker(config.tts_voice,
                               hold_while=lambda: self.recorder.recording)
        self.registry = SessionRegistry()
        self._transcribing = 0
        self._sending = 0
        self._flash_until = 0.0
        Overlay(self)

    def target_hwnd(self) -> int:
        """Overlay selection wins; otherwise focus tracking."""
        return self.registry.selected_hwnd or self.tracker.target

    def ui_state(self) -> dict:
        """Snapshot for the overlay: current phase + which row to highlight."""
        if self.recorder.recording:
            phase = "recording"
        elif self._sending:                # checked first: it nests inside
            phase = "sending"              # the transcribing span
        elif self._transcribing:
            phase = "transcribing"
        elif time.monotonic() < self._flash_until:
            phase = "flash"
        else:
            phase = "idle"
        highlight = self.registry.selected_pid
        if not highlight:
            hwnd = self.tracker.target
            for info in self.registry.list():
                if info["hwnd"] == hwnd:
                    highlight = info["pid"]
                    break
        return {"phase": phase, "highlight_pid": highlight}

    def pin_foreground(self) -> int:
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            self.tracker.pin(hwnd)
        return hwnd

    def toggle(self) -> None:
        if self.recorder.recording:
            audio = self.recorder.stop()
            # set BEFORE the worker starts: the overlay must never show a
            # blue idle gap between recording stop and transcription start
            self._transcribing += 1
            play_cue("record_stop")
            log.info("recording stopped (%.1fs), transcribing...",
                     audio.size / 16_000)
            threading.Thread(target=self._finish, args=(audio,),
                             daemon=True).start()
        else:
            self.speaker.interrupt()       # talking to Claude cuts Claude off
            self.recorder.start()
            play_cue("record_start")
            log.info("recording started")

    def _finish(self, audio) -> None:
        try:
            try:
                text = self.transcriber.transcribe(audio)
            except Exception:              # noqa: BLE001
                log.exception("transcription failed")
                return
            if not text:
                play_cue("error")
                log.info("empty transcript, nothing to inject")
                return
            self._sending += 1
            try:
                target = self.target_hwnd()
                if inject_text(target, MIC_PREFIX + text):
                    self._flash_until = time.monotonic() + 2.0
                    log.info("injected %d chars into hwnd %d",
                             len(text), target)
                else:
                    play_cue("error")
                    log.warning("no Claude Code window found to inject into")
            finally:
                self._sending -= 1
        finally:
            self._transcribing -= 1

    def run(self) -> None:
        modifiers = 0
        for name in self.config.hotkey_modifiers:
            modifiers |= MOD_FLAGS.get(name.lower(), 0)
        vk = ord(self.config.hotkey_key.upper())
        if not user32.RegisterHotKey(None, HOTKEY_ID,
                                     modifiers | MOD_NOREPEAT, vk):
            log.error("hotkey %s+%s is already taken by another program - "
                      "change it in %s", "+".join(self.config.hotkey_modifiers),
                      self.config.hotkey_key, config_dir() / "config.json")
            sys.exit(1)
        http_api.start(self, self.config.daemon_port)
        log.info("ready - hotkey %s+%s toggles recording",
                 "+".join(self.config.hotkey_modifiers),
                 self.config.hotkey_key)
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.toggle()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.platform != "win32":
        log.error("claude-code-ptt currently supports Windows only")
        sys.exit(1)
    Daemon(Config.load()).run()


if __name__ == "__main__":
    main()
