"""Registry of running Claude Code sessions.

Every session's MCP adapter registers itself here on startup (pid + cwd) and
keeps sending heartbeats; sessions without a heartbeat expire automatically.
The terminal window of a session is found by walking its parent-process chain
until an ancestor owns a visible top-level window.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import threading
import time
from pathlib import PureWindowsPath

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

TH32CS_SNAPPROCESS = 0x2
HEARTBEAT_TIMEOUT = 30.0

log = logging.getLogger("claude_code_ptt")


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260),
    ]


def _parent_map() -> dict[int, int]:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    parents: dict[int, int] = {}
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(_ProcessEntry)
    if kernel32.Process32First(snapshot, ctypes.byref(entry)):
        while True:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break
    kernel32.CloseHandle(snapshot)
    return parents


def _window_of_pid(pid: int) -> int:
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def on_window(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and not user32.GetParent(hwnd):
            owner = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(on_window, 0)
    return found[0] if found else 0


def window_title(hwnd: int) -> str:
    """Current title bar text of a window ('' if none)."""
    if not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(128)
    user32.GetWindowTextW(hwnd, buffer, 128)
    return buffer.value.strip()


def find_session_window(pid: int) -> int:
    """Walk the parent chain of pid until an ancestor - or one of an
    ancestor's direct children - owns a visible top-level window. The child
    check matters for classic consoles (cmd.exe): their visible window
    belongs to a conhost.exe CHILD of the shell, never to an ancestor."""
    parents = _parent_map()
    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    current = pid
    for _ in range(12):
        hwnd = _window_of_pid(current)
        if hwnd:
            return hwnd
        for child in children.get(current, []):
            if child != pid:
                hwnd = _window_of_pid(child)
                if hwnd:
                    return hwnd
        current = parents.get(current, 0)
        if current in (0, 4):
            break
    return 0


class SessionRegistry:
    """Registered sessions plus the user's explicit target selection."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[int, dict] = {}    # pid -> info
        self._turns: dict[str, dict] = {}       # transcript key -> turn state
        self.selected_pid = 0                   # 0 = automatic (focus tracking)
        threading.Thread(target=self._reaper, daemon=True).start()

    def register(self, pid: int, cwd: str, static: bool = False) -> dict:
        """Static sessions have no heartbeating adapter (e.g. a manually
        added window); they live until their window disappears."""
        label = PureWindowsPath(cwd).name or cwd
        hwnd = find_session_window(pid)
        with self._lock:
            self._sessions[pid] = {
                "pid": pid, "cwd": cwd, "label": label, "hwnd": hwnd,
                "static": static, "last_seen": time.monotonic(),
            }
        log.info("session registered: %s (pid=%d, hwnd=%d, static=%s)",
                 label, pid, hwnd, static)
        return self._sessions[pid]

    def heartbeat(self, pid: int) -> bool:
        with self._lock:
            info = self._sessions.get(pid)
            if info is None:
                return False
            info["last_seen"] = time.monotonic()
            return True

    def list(self) -> list[dict]:
        """Session snapshots, each with the live window title (the user
        names their terminals - the overlay shows that name)."""
        with self._lock:
            rows = [dict(info) for info in self._sessions.values()]
        for row in rows:
            row["title"] = window_title(row["hwnd"])[:60]
        return rows

    @staticmethod
    def _norm_cwd(cwd: str) -> str:
        """Windows paths are case-insensitive and hooks may report them with
        different casing or slashes than the adapter registered."""
        return os.path.normpath(cwd).casefold()

    def _best_pids(self, reported: str) -> list[int]:
        """Sessions a reported cwd belongs to. A session's shell may cd into
        subfolders, so any registered cwd containing the reported path
        matches - but only the most specific (longest) one wins, otherwise
        a session started in a parent folder swallows its children's
        reports. Call with the lock held."""
        rep = self._norm_cwd(reported)
        best_len = -1
        best: list[int] = []
        for pid, info in self._sessions.items():
            reg = self._norm_cwd(info["cwd"])
            if rep == reg or rep.startswith(reg + os.sep):
                if len(reg) > best_len:
                    best_len, best = len(reg), [pid]
                elif len(reg) == best_len:
                    best.append(pid)
        return best

    def set_turn_state(self, transcript: str, busy: bool) -> None:
        """Turn state keyed by transcript path - the only per-session-unique
        identity the hooks can report (sibling sessions may share a cwd, so
        a folder-based busy flag would be clobbered by their turn ends)."""
        key = os.path.normcase(transcript)
        with self._lock:
            self._turns[key] = {"busy": busy, "path": transcript,
                                "ts": time.monotonic()}
            if len(self._turns) > 32:      # prune long-gone sessions
                oldest = min(self._turns, key=lambda k: self._turns[k]["ts"])
                del self._turns[oldest]

    def is_busy_transcript(self, transcript: str) -> bool:
        if not transcript:
            return False
        with self._lock:
            state = self._turns.get(os.path.normcase(transcript))
            return bool(state and state["busy"])

    def candidate_transcripts(self, pid: int) -> list[str]:
        """Transcripts a send could show up in: the session's own hint
        first, then every other known one (the hint may point to a sibling
        when several sessions share a cwd)."""
        candidates: list[str] = []
        hint = self.transcript_for(pid)
        if hint:
            candidates.append(hint)
        with self._lock:
            for state in self._turns.values():
                if state["path"] not in candidates:
                    candidates.append(state["path"])
            for info in self._sessions.values():
                path = str(info.get("transcript") or "")
                if path and path not in candidates:
                    candidates.append(path)
        return candidates

    def set_transcript(self, cwd: str, path: str) -> None:
        """Transcript file hint per session, reported by its hooks."""
        with self._lock:
            for pid in self._best_pids(cwd):
                self._sessions[pid]["transcript"] = path

    def transcript_for(self, pid: int) -> str:
        with self._lock:
            info = self._sessions.get(pid)
            return str(info.get("transcript") or "") if info else ""

    def session_for_cwd(self, cwd: str) -> dict | None:
        with self._lock:
            pids = self._best_pids(cwd)
            return dict(self._sessions[pids[0]]) if pids else None

    def select(self, pid: int) -> None:
        """Overlay click: pin this session as the PTT target."""
        self.selected_pid = pid

    def effective_pid(self) -> int:
        """The acting target: the clicked session - or, when exactly one
        session exists, that one automatically."""
        with self._lock:
            if self.selected_pid in self._sessions:
                return self.selected_pid
            if len(self._sessions) == 1:
                return next(iter(self._sessions))
        return 0

    def unregister(self, pid: int) -> None:
        """Session closes: drop it; a pinned target falls back to auto."""
        with self._lock:
            info = self._sessions.pop(pid, None)
        if info:
            log.info("session unregistered: %s (pid=%d)", info["label"], pid)
        if self.selected_pid == pid:
            self.selected_pid = 0

    @property
    def selected_hwnd(self) -> int:
        pid = self.effective_pid()
        with self._lock:
            info = self._sessions.get(pid)
        if not info:
            return 0
        if not info["hwnd"] or not user32.IsWindow(info["hwnd"]):
            info["hwnd"] = find_session_window(info["pid"])
        return info["hwnd"]

    def _reaper(self) -> None:
        while True:
            time.sleep(5)
            cutoff = time.monotonic() - HEARTBEAT_TIMEOUT
            with self._lock:
                dead = []
                for pid, info in self._sessions.items():
                    if info.get("static"):
                        if not user32.IsWindow(info["hwnd"]):
                            dead.append(pid)
                    elif info["last_seen"] < cutoff:
                        dead.append(pid)
                for pid in dead:
                    log.info("session expired: %s (pid=%d)",
                             self._sessions[pid]["label"], pid)
                    del self._sessions[pid]
                if self.selected_pid in dead:
                    self.selected_pid = 0
