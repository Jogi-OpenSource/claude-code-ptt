"""Target-window tracking and keyboard injection (Win32 via ctypes)."""
import ctypes
import ctypes.wintypes as wt
import threading
import time

user32 = ctypes.windll.user32

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D


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


def _send_inputs(inputs: list[_Input]) -> None:
    array = (_Input * len(inputs))(*inputs)
    user32.SendInput(len(inputs), array, ctypes.sizeof(_Input))


def _char_inputs(char: str) -> list[_Input]:
    code = ord(char)
    down = _Input(type=INPUT_KEYBOARD)
    down.union.ki = _KeyboardInput(0, code, KEYEVENTF_UNICODE, 0, None)
    up = _Input(type=INPUT_KEYBOARD)
    up.union.ki = _KeyboardInput(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                                 0, None)
    return [down, up]


def inject_text(hwnd: int, text: str, press_enter: bool = True) -> bool:
    """Focus the window and type the text as Unicode key events."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)                       # give focus a moment to settle
    inputs: list[_Input] = []
    for char in text:
        inputs.extend(_char_inputs(char))
    _send_inputs(inputs)
    if press_enter:
        time.sleep(0.05)
        down = _Input(type=INPUT_KEYBOARD)
        down.union.ki = _KeyboardInput(VK_RETURN, 0, 0, 0, None)
        up = _Input(type=INPUT_KEYBOARD)
        up.union.ki = _KeyboardInput(VK_RETURN, 0, KEYEVENTF_KEYUP, 0, None)
        _send_inputs([down, up])
    return True
