"""Resolve the real terminal window of a console process.

Run as a short-lived helper:  python -m claude_code_ptt.console_window <pid>
Prints the visible top-level window handle (or 0).

Attaching to another process's console only works from a process WITHOUT
its own console, which is why the daemon shells out to this module instead
of calling it in-process (it would lose its log console). Under Windows
Terminal the console's own window is a hidden PseudoConsoleWindow whose
OWNER is the real, visible CASCADIA host window - the terminal window the
user sees, even when the shell was launched elsewhere (default-terminal
delegation puts no Windows Terminal process into the session's ancestry).
"""
import ctypes
import sys

GA_ROOTOWNER = 3


def resolve(pid: int) -> int:
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return 0
    try:
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return 0
        # The pseudo console window claims to be "visible", so visibility
        # cannot discriminate - the root owner is the real terminal window
        # under Windows Terminal and the window itself for classic conhost.
        root = user32.GetAncestor(hwnd, GA_ROOTOWNER) or hwnd
        return root if user32.IsWindowVisible(root) else 0
    finally:
        kernel32.FreeConsole()


def main() -> None:
    try:
        print(resolve(int(sys.argv[1])))
    except (IndexError, ValueError):
        print(0)


if __name__ == "__main__":
    main()
