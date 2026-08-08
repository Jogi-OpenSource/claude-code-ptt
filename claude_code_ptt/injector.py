"""Target-window tracking and text injection (Win32 via ctypes).

Injection strategy (battle-tested): put the text on the clipboard, focus the
target window, paste with Ctrl+V, wait briefly, then press Enter. A plain
SetForegroundWindow from a background process is refused by Windows, so an
Alt keypress precedes it and AttachThreadInput to the current foreground
thread serves as fallback. Focus and clipboard are restored afterwards.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
SW_RESTORE = 9
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

log = logging.getLogger("claude_code_ptt")


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyboardInput), ("padding", ctypes.c_byte * 32)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _InputUnion)]


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


class TargetTracker:
    """Remembers the most recent foreground window that looks like Claude Code.

    Polling GetForegroundWindow (4x/s) is deliberate: a WinEvent hook would
    need its own message pump and brings no practical benefit here.
    An explicitly pinned window (via /ptt-here) always wins until unpinned.
    """

    def __init__(self, title_markers: list[str]):
        self._markers = [m.lower() for m in title_markers]
        self._last_match = 0
        self._pinned = 0
        threading.Thread(target=self._poll, daemon=True).start()

    def pin(self, hwnd: int) -> None:
        self._pinned = hwnd

    def unpin(self) -> None:
        self._pinned = 0

    @property
    def target(self) -> int:
        if self._pinned and user32.IsWindow(self._pinned):
            return self._pinned
        return self._last_match

    def _poll(self) -> None:
        while True:
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                title = _window_title(hwnd).lower()
                if any(m in title for m in self._markers):
                    self._last_match = hwnd
            time.sleep(0.25)


# ------------------------------------------------------------------ clipboard
def _clipboard_get() -> str:
    text = ""
    if not user32.OpenClipboard(None):
        return text
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if handle:
            pointer = kernel32.GlobalLock(handle)
            if pointer:
                text = ctypes.wstring_at(pointer)
                kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    return text


def _clipboard_set(text: str) -> bool:
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, size)
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


# ------------------------------------------------------------------ keyboard
def _key(vk: int, flags: int = 0) -> _Input:
    item = _Input(type=INPUT_KEYBOARD)
    item.union.ki = _KeyboardInput(vk, 0, flags, 0, None)
    return item


def _send_keys(inputs: list[_Input]) -> None:
    array = (_Input * len(inputs))(*inputs)
    user32.SendInput(len(inputs), array, ctypes.sizeof(_Input))


# ------------------------------------------------------------------ focusing
def _ensure_focus(hwnd: int) -> bool:
    """Focus hwnd; piggyback on the foreground thread's input priority if
    Windows refuses (the well-known AttachThreadInput trick)."""
    if user32.GetForegroundWindow() == hwnd:
        return True
    current_thread = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_thread and fg_thread != current_thread:
        attached = bool(user32.AttachThreadInput(current_thread, fg_thread,
                                                 True))
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.3)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, fg_thread, False)
    time.sleep(0.2)
    return user32.GetForegroundWindow() == hwnd


def inject_text(hwnd: int, text: str, press_enter: bool = True) -> bool:
    """Paste text into the window via clipboard and optionally press Enter."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False

    old_clipboard = _clipboard_get()
    if not _clipboard_set(text):
        log.warning("could not write to clipboard")
        return False

    prev_fg = user32.GetForegroundWindow()

    # Alt keypress grants a background process the right to set foreground.
    _send_keys([_key(VK_MENU), _key(VK_MENU, KEYEVENTF_KEYUP)])
    time.sleep(0.05)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.3)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    if not _ensure_focus(hwnd):
        log.warning("could not focus target window %d", hwnd)
        _clipboard_set(old_clipboard)
        return False

    _send_keys([_key(VK_CONTROL), _key(VK_V),
                _key(VK_V, KEYEVENTF_KEYUP),
                _key(VK_CONTROL, KEYEVENTF_KEYUP)])
    time.sleep(0.15)
    if press_enter:
        _send_keys([_key(VK_RETURN), _key(VK_RETURN, KEYEVENTF_KEYUP)])

    # Give the user their previous window and clipboard back.
    if prev_fg and prev_fg != hwnd and user32.IsWindow(prev_fg):
        time.sleep(0.1)
        _ensure_focus(prev_fg)

    def _restore_clipboard():
        time.sleep(0.5)
        _clipboard_set(old_clipboard)

    threading.Thread(target=_restore_clipboard, daemon=True).start()
    return True
