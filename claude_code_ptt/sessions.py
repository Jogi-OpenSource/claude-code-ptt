"""Registry of running Claude Code sessions.

Every session's MCP adapter registers itself here on startup (pid + cwd) and
keeps sending heartbeats; sessions without a heartbeat expire automatically.
The terminal window of a session is found by walking its parent-process chain
until an ancestor owns a visible top-level window.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import itertools
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import PureWindowsPath

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

TH32CS_SNAPPROCESS = 0x2
HEARTBEAT_TIMEOUT = 30.0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# handles are pointers; without a restype ctypes truncates them to int
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
kernel32.GetProcessTimes.argtypes = (
    (ctypes.c_void_p,) + (ctypes.POINTER(wt.FILETIME),) * 4)

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


def process_tree() -> dict[int, tuple[int, str]]:
    """Snapshot of all processes: pid -> (parent pid, lowercased exe name)."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    tree: dict[int, tuple[int, str]] = {}
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(_ProcessEntry)
    if kernel32.Process32First(snapshot, ctypes.byref(entry)):
        while True:
            name = entry.szExeFile.decode("mbcs", errors="replace").lower()
            tree[entry.th32ProcessID] = (entry.th32ParentProcessID, name)
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break
    kernel32.CloseHandle(snapshot)
    return tree


def process_start_time(pid: int) -> int:
    """When this process was created, as a raw FILETIME (0 = cannot tell).

    Windows hands pids out again: the number of a session that ended can
    belong to a stranger minutes later. Pid plus creation time is the
    identity that survives that - it tells 'the session the user picked'
    from 'whatever runs under its number now', and nothing else the
    registry sees does (cwd and label are shared by sibling sessions).

    0 means the question could not be answered - the process is gone, or
    the handle was refused. 0 is NOT an identity: two processes the
    question failed for are not thereby the same one. The registry never
    stores it, it substitutes a stamp of its own (see
    SessionRegistry._identity_stamp)."""
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0
    created, exited, in_kernel, in_user = (wt.FILETIME() for _ in range(4))
    ok = kernel32.GetProcessTimes(
        handle, ctypes.byref(created), ctypes.byref(exited),
        ctypes.byref(in_kernel), ctypes.byref(in_user))
    kernel32.CloseHandle(handle)
    if not ok:
        return 0
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


MIN_WINDOW_SIZE = 50
MONITOR_DEFAULTTONULL = 0


def _plausible_window(hwnd: int) -> bool:
    """A real terminal window, not a helper: helper windows can be
    'visible' at 1x1 (seen live: explorer's ThumbnailDeviceHelperWnd) or
    parked far off-screen (ConPTY hosting windows at -25600)."""
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    if (rect.right - rect.left < MIN_WINDOW_SIZE
            or rect.bottom - rect.top < MIN_WINDOW_SIZE):
        return False
    return bool(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONULL))


def _window_of_pid(pid: int) -> int:
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def on_window(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and not user32.GetParent(hwnd):
            owner = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and _plausible_window(hwnd):
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


CONSOLE_HOSTS = {"conhost.exe", "openconsole.exe"}


def find_session_window(pid: int) -> int:
    """The console probe comes FIRST: it asks the session's own console for
    its window and can never grab an unrelated helper (the tree walk once
    delivered into a 1x1 explorer helper window). The walk over ancestors -
    and their console-host children, for classic conhost consoles - stays
    as the fallback for sessions the probe cannot see (e.g. Electron glass
    terminals, whose adapters register without an attachable console)."""
    hwnd = _console_window_probe(pid)
    if hwnd and _plausible_window(hwnd):
        return hwnd
    tree = process_tree()
    children: dict[int, list[int]] = {}
    for child, (parent, _name) in tree.items():
        children.setdefault(parent, []).append(child)
    current = pid
    for _ in range(12):
        hwnd = _window_of_pid(current)
        if hwnd:
            return hwnd
        for child in children.get(current, []):
            if tree.get(child, (0, ""))[1] in CONSOLE_HOSTS:
                hwnd = _window_of_pid(child)
                if hwnd:
                    return hwnd
        current = tree.get(current, (0, ""))[0]
        if current in (0, 4):
            break
    return 0


def _console_window_probe(pid: int) -> int:
    """Deterministic fallback: attach to the session's console and take the
    owner of its (possibly hidden) console window - finds the real Windows
    Terminal window even when it is unrelated to the session's ancestry.
    Runs as a hidden helper process because attaching requires having no
    console of one's own (the daemon keeps its log console)."""
    try:
        probe = subprocess.run(
            [sys.executable, "-m", "claude_code_ptt.console_window",
             str(pid)],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW)
        hwnd = int(probe.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0
    if hwnd:
        log.info("window for pid %d resolved via console probe: %d",
                 pid, hwnd)
    return hwnd


class SessionRegistry:
    """Registered sessions plus the user's explicit target selection."""

    def __init__(self, voices):
        """Voices are not handed out here - voices.VoicePool does that
        when a session speaks. The registry only tells the pool which
        processes still exist: a voice may be reused once the process
        holding it is gone, and the reaper is the daemon's standing sweep
        for exactly that question."""
        self._lock = threading.Lock()
        self._sessions: dict[int, dict] = {}    # pid -> info
        self._turns: dict[str, dict] = {}       # transcript key -> turn state
        self._ranks: dict[tuple[int, int], float] = {}   # identity -> 1st seen
        self._voices = voices
        self._pin = (0, 0)                      # identity, (0, 0) = automatic
        self._unknown = itertools.count(1)      # stand-in identity stamps
        threading.Thread(target=self._reaper, daemon=True).start()

    def _identity_stamp(self, pid: int) -> int:
        """The second half of a session's identity - never 0.

        Creation time is the honest answer, but Windows may refuse it (the
        handle denied, the process already gone) and process_start_time()
        then reports 0. Storing that 0 would quietly shrink the identity
        back to the bare pid: a stranger registering under a recycled
        number answers 0 as well, compares equal, and inherits the user's
        pick - the very thing the pin exists to prevent.

        An unanswerable question therefore gets a stand-in: a counter,
        negative so it can never collide with a real FILETIME, unique per
        registration. Two unknowns never compare equal again. The price is
        deliberate - while the creation time stays unreadable, a session
        that re-registers counts as a new one and the target falls back to
        the main session. Losing the pick costs one click; dictating into
        a stranger's session cannot be taken back."""
        return process_start_time(pid) or -next(self._unknown)

    def register(self, pid: int, cwd: str, static: bool = False) -> dict:
        """Static sessions have no heartbeating adapter (e.g. a manually
        added window); they live until their window disappears."""
        label = PureWindowsPath(cwd).name or cwd
        hwnd = find_session_window(pid)
        started = self._identity_stamp(pid)
        with self._lock:
            # First registration wins, and it is remembered per session
            # identity instead of per entry: a heartbeat lapse deletes the
            # entry outright (see _reap), and the adapter re-registers
            # seconds later. Ranked by that second stamp, the main session
            # would come back as the YOUNGEST of all - and _main_pid()
            # would hand the automatic target to a spawned sub-session.
            # Keyed by identity, a reused pid gets a fresh rank instead of
            # inheriting the dead session's seniority.
            rank = self._ranks.setdefault((pid, started), time.monotonic())
            self._sessions[pid] = {
                "pid": pid, "cwd": cwd, "label": label, "hwnd": hwnd,
                "static": static, "last_seen": time.monotonic(),
                "started": started, "registered": rank,
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
        """Overlay click: pin this session as the PTT target.

        This is a LATCH - the user's click is the ONLY thing that moves it
        (pid 0 = automatic mode). Nothing in the daemon may overwrite it:
        not a session registering, not one ending, not focus. The single
        automatic change is described in effective_pid().

        Pinned is the session, not its number: the pid is stored with the
        creation time of the process behind it, so a stranger that later
        inherits the pid does not inherit the click. A pid nobody can put
        a creation time to stays unpinned (automatic) rather than pinned
        to a number - the overlay only ever offers registered sessions,
        which always carry an identity of their own."""
        with self._lock:
            info = self._sessions.get(pid)
            started = info["started"] if info else process_start_time(pid)
            self._pin = (pid, started) if pid and started else (0, 0)

    @staticmethod
    def _pick_still_running(pinned: int, pin_started: int,
                            alive: set[int]) -> bool:
        """Is the process behind the user's pick still there?

        The honest answer is its creation time - pid AND stamp, so a
        recycled number cannot answer for the session that is gone. When
        Windows refuses that question (process_start_time() == 0, see
        there) the process list is the evidence that remains, and it
        settles the case: a pid that is still running has not
        disappeared, whatever its adapter is doing. Treating the silence
        as 'some other process' instead would expire a session that is
        plainly still up, on nothing but a heartbeat lapse - and hand
        dictation to the main session, which is the silent repointing the
        latch exists to prevent.

        A readable time is still taken at its word, including against a
        stand-in stamp: rights on a process do not change while it runs,
        so a time that reads now where it did not before belongs to a
        different process. Only a successor that refuses the question as
        well can slip through, and it inherits nothing by doing so - the
        pick's identity is checked again in effective_pid(), and a
        stranger registering under the number overwrites the entry with a
        stamp of its own."""
        now = process_start_time(pinned)
        if now:
            return now == pin_started
        return pinned in alive

    def _main_pid(self) -> int:
        """The 'main session': the one that registered first and is still
        alive. The registry knows nothing about a session's role - the
        adapter reports only pid and cwd - so the oldest registration is
        the simplest robust marker: the terminal the user works in is up
        before the sessions it spawns (sub-agents, worker sessions).
        Call with the lock held."""
        if not self._sessions:
            return 0
        return min(self._sessions,
                   key=lambda pid: self._sessions[pid]["registered"])

    def effective_pid(self) -> int:
        """The acting target: the session the user clicked, for as long as
        it exists. Sessions coming and going never move it.

        Only two things change the target: another click, or the chosen
        session being gone - then it falls back to the main session. Gone
        means its process ended; a silent adapter is not a gone session
        (see _reap). In
        automatic mode (nothing clicked) the main session is the target;
        focus tracking does not exist any more (removed in e608548), so
        'automatic' means exactly that fallback.

        'Exists' means the pinned session, not merely its pid: a session
        registering under a recycled pid is a stranger and gets the
        fallback treatment, however familiar its number looks."""
        with self._lock:
            pid, started = self._pin
            info = self._sessions.get(pid)
            if info is not None and info["started"] == started:
                return pid
            return self._main_pid()

    def unregister(self, pid: int) -> None:
        """Session says goodbye: drop it. The pin STAYS on that session -
        if it comes back (adapter restart, re-register), it is the target
        again; while it is gone, effective_pid() serves the main
        session. The voice stays reserved for the same reason: a goodbye
        comes from the adapter, and a restarted adapter says it for a
        session that keeps running. Only the reaper hands voices of gone
        processes back."""
        with self._lock:
            info = self._sessions.pop(pid, None)
        if info:
            log.info("session unregistered: %s (pid=%d)", info["label"], pid)

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
            self._reap()

    def _reap(self) -> None:
        """Drop sessions whose heartbeats stopped (static ones: whose
        window is gone), then free the voices of the processes that are
        really gone. A missed heartbeat is not a goodbye - the adapter
        re-registers moments later and has to keep sounding the same, keep
        its rank as the main session (see register()), and the pin is left
        alone here too (see unregister())."""
        cutoff = time.monotonic() - HEARTBEAT_TIMEOUT
        alive = set(process_tree())
        with self._lock:
            pinned, pin_started = self._pin
            # The session the user picked is held to a stricter test than
            # the rest: it expires when its PROCESS is gone, not when its
            # adapter stops talking. A heartbeat lapse is the adapter's
            # lapse; expiring the pinned entry on one would move dictation
            # to the main session while the picked session is still up and
            # running - the exact silent repointing the latch exists to
            # prevent. Asked of the process itself, not of the entry -
            # by creation time where Windows gives one, by the process
            # list where it does not (see _pick_still_running).
            pin_running = (pinned in self._sessions
                           and self._pick_still_running(
                               pinned, pin_started, alive))
            dead = []
            for pid, info in self._sessions.items():
                if info.get("static"):
                    if not user32.IsWindow(info["hwnd"]):
                        dead.append(pid)
                elif info["last_seen"] < cutoff:
                    if pid == pinned and pin_running:
                        continue
                    dead.append(pid)
            for pid in dead:
                log.info("session expired: %s (pid=%d)",
                         self._sessions[pid]["label"], pid)
                del self._sessions[pid]
            # Ranks outlive their entry on purpose, but only while the
            # process can still come back - this is what keeps the map
            # from growing for the whole life of the daemon.
            for identity in [i for i in self._ranks if i[0] not in alive]:
                del self._ranks[identity]
        self._voices.retain(alive)
