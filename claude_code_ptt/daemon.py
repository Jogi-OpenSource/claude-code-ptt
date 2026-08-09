"""The claude-code-ptt daemon: hotkey -> record -> transcribe -> inject.

Single instance per machine. Owns the global hotkey (default Ctrl+M) and the
microphone; the MCP adapter talks to it over localhost HTTP.

Delivery model: the target is ALWAYS an explicitly clicked session in the
overlay - there is no focus tracking. A transcript without a valid target is
held back ("ZIEL WAEHLEN") until the user picks one. After injection the
daemon waits for the session's confirm hook to report the prompt as actually
processed; only that counts as delivered ("ANGEKOMMEN"). No confirmation
within CONFIRM_TIMEOUT means delivery failure ("NICHT ANGEKOMMEN").
"""
import ctypes
import ctypes.wintypes
import faulthandler
import logging
import os
import sys
import threading
import time

from . import http_api
from .config import Config, config_dir
from .cues import play_cue
from .injector import inject_text
from .mic_mute import MicMute
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
MIC_PREFIX = "[mic] "                      # ASCII: renders in every terminal
CONFIRM_TIMEOUT = 8.0
FLASH_SECONDS = 2.0

log = logging.getLogger("claude_code_ptt")


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        self.recorder = Recorder()
        self.mic_mute = MicMute()
        self.transcriber = Transcriber(config.whisper_model, config.language)
        self.speaker = Speaker(config.tts_voice,
                               hold_while=lambda: self.recorder.recording)
        self.registry = SessionRegistry()
        self._transcribing = 0
        self._sending = 0
        self._pending_text = None          # transcript waiting for a target
        self._await_text = None            # injected text awaiting confirm
        self._await_until = 0.0
        self._failed = False
        self._flash_until = 0.0
        threading.Thread(target=self._confirm_watchdog, daemon=True).start()
        Overlay(self)

    def target_hwnd(self) -> int:
        """The explicitly selected session's window; 0 if none."""
        return self.registry.selected_hwnd

    def ui_state(self) -> dict:
        """Snapshot for the overlay: current phase + which row to highlight."""
        if self.recorder.recording:
            phase = "recording"
        elif self._sending or self._await_text is not None:
            phase = "sending"
        elif self._transcribing:
            phase = "transcribing"
        elif self._pending_text is not None:
            phase = "choose_target"
        elif self._failed:
            phase = "failed"
        elif time.monotonic() < self._flash_until:
            phase = "flash"
        else:
            phase = "idle"
        return {"phase": phase,
                "highlight_pid": self.registry.effective_pid(),
                "speaking_pid": self.speaker.speaking_origin}

    def select_target(self, pid: int) -> None:
        """Overlay click: choose the target; deliver a held transcript."""
        self.registry.select(pid)
        pending, self._pending_text = self._pending_text, None
        if pending is not None and self.registry.selected_hwnd:
            threading.Thread(target=self._deliver_wrapped, args=(pending,),
                             daemon=True).start()
        elif pending is not None:
            self._pending_text = pending   # clicked row has no window

    def toggle(self) -> None:
        if self.recorder.recording:
            audio = self.recorder.stop()
            self.mic_mute.restore()
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
            self._failed = False           # new attempt clears the fail state
            self._pending_text = None
            self.mic_mute.open_for_recording()
            self.recorder.start()
            play_cue("record_start")
            log.info("recording started")

    def confirm_received(self, prompt: str) -> bool:
        """Confirm hook reported a processed prompt; match it to our send."""
        awaited = self._await_text
        if awaited is None:
            log.info("confirm ignored, nothing awaited (prompt head=%r)",
                     prompt[:60])
            return False
        if awaited[:60] in prompt:
            self._await_text = None
            self._failed = False
            self._flash_until = time.monotonic() + FLASH_SECONDS
            log.info("delivery confirmed by session")
            return True
        log.warning("confirm MISMATCH: awaited=%r vs prompt head=%r",
                    awaited[:60], prompt[:80])
        return False

    def _confirm_watchdog(self) -> None:
        while True:
            time.sleep(0.5)
            if (self._await_text is not None
                    and time.monotonic() > self._await_until):
                self._await_text = None
                self._failed = True
                play_cue("error")
                log.warning("delivery NOT confirmed - text may be stuck "
                            "in the input line")

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
            if not self.registry.selected_hwnd:
                self._pending_text = text
                play_cue("error")
                log.info("no target selected - transcript held back")
                return
            self._deliver_wrapped(text)
        finally:
            self._transcribing -= 1

    def _deliver_wrapped(self, text: str) -> None:
        self._sending += 1
        try:
            self._deliver(text)
        finally:
            self._sending -= 1

    def _deliver(self, text: str) -> None:
        target = self.registry.selected_hwnd
        injected = MIC_PREFIX + text
        if inject_text(target, injected):
            self._await_text = injected
            self._await_until = time.monotonic() + CONFIRM_TIMEOUT
            log.info("injected %d chars into hwnd %d, awaiting confirmation",
                     len(text), target)
        else:
            self._failed = True
            play_cue("error")
            log.warning("injection failed for hwnd %d", target)

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
    log_dir = config_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "daemon.log", encoding="utf-8"),
        ],
    )
    # Native crashes (access violations in ctypes/Win32 calls) kill the
    # process without a Python traceback; faulthandler dumps the stacks of
    # all threads to crash.log. The file object must stay referenced for
    # the daemon's lifetime, so it is kept on the module.
    global _crash_log
    _crash_log = open(log_dir / "crash.log", "a", encoding="utf-8")
    _crash_log.write(f"--- daemon start {time.strftime('%Y-%m-%d %H:%M:%S')}"
                     f" (pid={os.getpid()}) ---\n")
    _crash_log.flush()
    faulthandler.enable(_crash_log, all_threads=True)
    if sys.platform != "win32":
        log.error("claude-code-ptt currently supports Windows only")
        sys.exit(1)
    Daemon(Config.load()).run()


if __name__ == "__main__":
    main()
