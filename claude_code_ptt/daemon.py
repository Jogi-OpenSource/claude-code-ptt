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
import json
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
QUEUE_GRACE = 5.0                          # after turn end: time to confirm
QUEUE_MAX = 60 * 60.0                      # give up on a queued prompt after
MAX_PENDING_SENDS = 8
TRANSCRIPT_TAIL = 120


def _transcript_texts(path: str) -> tuple[list[str], list[str]]:
    """Tail of a session transcript, split into texts still waiting in the
    session's queue (enqueue entries) and texts of processed user messages.
    A consumed interjection appears as both, enqueue first."""
    queued: list[str] = []
    processed: list[str] = []
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()[-TRANSCRIPT_TAIL:]
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if (entry.get("type") == "queue-operation"
                and entry.get("operation") == "enqueue"):
            queued.append(str(entry.get("content") or ""))
            continue
        if entry.get("type") != "user":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            processed.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    processed.append(str(part.get("text") or ""))
    return queued, processed

log = logging.getLogger("claude_code_ptt")


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        self.recorder = Recorder()
        self.mic_mute = MicMute()
        self.transcriber = Transcriber(config.whisper_model, config.language,
                                       config.whisper_hotwords)
        self.speaker = Speaker(config.tts_voice,
                               hold_while=lambda: self.recorder.recording)
        self.registry = SessionRegistry()
        self._transcribing = 0
        self._sending = 0
        self._pending_text = None          # transcript waiting for a target
        # Every injected text is tracked as its own pending send until it is
        # confirmed or fails - rapid follow-up messages must not steal the
        # tracking slot of an earlier, still unconfirmed one.
        self._sends: list[dict] = []
        self._sends_lock = threading.Lock()
        self._failed = False
        self._flash_until = 0.0
        threading.Thread(target=self._confirm_watchdog, daemon=True).start()
        threading.Thread(target=self._transcript_watchdog,
                         daemon=True).start()
        Overlay(self)

    def target_hwnd(self) -> int:
        """The explicitly selected session's window; 0 if none."""
        return self.registry.selected_hwnd

    def ui_state(self) -> dict:
        """Snapshot for the overlay: current phase + which row to highlight."""
        with self._sends_lock:
            any_sending = any(not s["queued"] for s in self._sends)
            any_queued = any(s["queued"] for s in self._sends)
        if self.recorder.recording:
            phase = "recording"
        elif self._sending or any_sending:
            phase = "sending"
        elif any_queued:
            phase = "queued"
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

    def _confirm_send(self, send: dict, source: str) -> None:
        with self._sends_lock:
            if send in self._sends:
                self._sends.remove(send)
        self._failed = False
        self._flash_until = time.monotonic() + FLASH_SECONDS
        log.info("delivery confirmed %s", source)

    def confirm_received(self, prompt: str, cwd: str = "") -> bool:
        """Confirm hook reported a processed prompt; match it to our sends."""
        sender = self.registry.session_for_cwd(cwd) if cwd else None
        sender_label = (f"{sender['label']} (pid={sender['pid']})"
                        if sender else "unknown session")
        with self._sends_lock:
            sends = list(self._sends)
        if not sends:
            log.info("confirm ignored, nothing awaited (from %s, "
                     "prompt head=%r)", sender_label, prompt[:60])
            return False
        for send in sends:
            if send["text"][:60] in prompt:
                self._confirm_send(send, f"by {sender_label}")
                return True
        log.warning("confirm MISMATCH: awaited=%s vs prompt head=%r",
                    [s["text"][:40] for s in sends], prompt[:80])
        return False

    def _transcript_watchdog(self) -> None:
        """Watch the target sessions' transcripts while sends are awaiting.
        An enqueue entry proves a text reached the session's queue (shown
        as WARTESCHLANGE); only a processed user message counts as
        delivered. Processed wins: a consumed interjection leaves both."""
        while True:
            time.sleep(1.0)
            with self._sends_lock:
                sends = list(self._sends)
            transcripts: dict[int, tuple[list[str], list[str]] | None] = {}
            for send in sends:
                pid = send["pid"]
                if pid not in transcripts:
                    path = self.registry.transcript_for(pid)
                    try:
                        transcripts[pid] = (_transcript_texts(path)
                                            if path else None)
                    except (OSError, ValueError):
                        transcripts[pid] = None
                tail = transcripts[pid]
                if tail is None:
                    continue
                queued, processed = tail
                head = send["text"][:60]
                if any(head in text for text in processed[-12:]):
                    self._confirm_send(
                        send, "via transcript (processed by session)")
                elif (not send["queued"]
                        and any(head in text for text in queued[-12:])):
                    send["queued"] = True
                    log.info("arrived in the session queue - waiting to "
                             "be processed")

    def _confirm_watchdog(self) -> None:
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            with self._sends_lock:
                sends = list(self._sends)
            for send in sends:
                age = now - send["started"]
                busy = self.registry.is_busy(send["pid"])
                if send["queued"]:
                    # Proven queued: survives while its session works; once
                    # the session idles it must confirm within the grace
                    # window or it really got lost.
                    if busy:
                        send["idle_since"] = None
                        if age < QUEUE_MAX:
                            continue
                    else:
                        if send["idle_since"] is None:
                            send["idle_since"] = now
                        if now - send["idle_since"] < QUEUE_GRACE:
                            continue
                elif now <= send["deadline"] or (busy and age < QUEUE_MAX):
                    # Within the normal window, or the session is mid-turn
                    # and the transcript may simply lag.
                    continue
                with self._sends_lock:
                    if send in self._sends:
                        self._sends.remove(send)
                self._failed = True
                play_cue("error")
                log.warning("delivery NOT confirmed - text may be stuck "
                            "in the input line (head=%r)",
                            send["text"][:40])

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
        pid = self.registry.effective_pid()
        target = self.registry.selected_hwnd
        injected = MIC_PREFIX + text
        if inject_text(target, injected):
            now = time.monotonic()
            with self._sends_lock:
                self._sends.append({
                    "text": injected, "pid": pid, "started": now,
                    "deadline": now + CONFIRM_TIMEOUT,
                    "queued": False, "idle_since": None,
                })
                if len(self._sends) > MAX_PENDING_SENDS:
                    dropped = self._sends.pop(0)
                    log.warning("too many pending sends, dropping oldest "
                                "(head=%r)", dropped["text"][:40])
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
