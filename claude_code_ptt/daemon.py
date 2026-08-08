"""The claude-code-ptt daemon: hotkey -> record -> transcribe -> inject.

Single instance per machine. Owns the global hotkey (default Ctrl+M) and the
microphone; the MCP adapter talks to it over localhost HTTP (Phase 2).
"""
import ctypes
import ctypes.wintypes
import logging
import sys
import threading

from . import http_api
from .config import Config, config_dir
from .cues import play_cue
from .injector import TargetTracker, inject_text
from .recorder import Recorder
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

    def pin_foreground(self) -> int:
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            self.tracker.pin(hwnd)
        return hwnd

    def toggle(self) -> None:
        if self.recorder.recording:
            audio = self.recorder.stop()
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
            text = self.transcriber.transcribe(audio)
        except Exception:                  # noqa: BLE001
            log.exception("transcription failed")
            return
        if not text:
            play_cue("error")
            log.info("empty transcript, nothing to inject")
            return
        target = self.tracker.target
        if inject_text(target, MIC_PREFIX + text):
            log.info("injected %d chars into hwnd %d", len(text), target)
        else:
            log.warning("no Claude Code window found to inject into")

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
