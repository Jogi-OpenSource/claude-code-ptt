"""Which localhost port the daemon of THIS Windows session listens on.

Loopback belongs to the whole machine, the desktop does not: with a second
account logged on, its daemon answers on the configured port just fine,
while its overlay sits on a desktop nobody here can see and its log is
written into that account's profile. Attaching to it leaves this session
mute. The adapter therefore asks the daemon which Windows session it runs
in (see the /status endpoint) and starts one of its own when the answer is
a stranger's.

Which port that daemon ends up on is decided by its bind, never by a
lookup beforehand (see http_api.start): both accounts choose in the same
moment, a lookup answers both the same free port, and the second bind then
fails - leaving that session without a daemon. The daemon notes the port
it really bound down here - beside the config, but per Windows session,
because two sessions of the same account share the config file. The
adapter and both hooks follow that note, so all four parts keep finding
each other.

A machine with a single logged-on account never writes the note and stays
on the configured port.
"""
import ctypes
import sys
from pathlib import Path

from .config import config_dir

INSTANCE_MUTEX = "Local\\claude-code-ptt-daemon"   # Local\ = this session
ERROR_ALREADY_EXISTS = 183

_instance_handle = None                    # held for the process's lifetime


def session_id() -> int:
    """Windows terminal-services session of this process (0 if unknown)."""
    if sys.platform != "win32":
        return 0
    kernel32 = ctypes.windll.kernel32
    sid = ctypes.c_ulong()
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(),
                                         ctypes.byref(sid)):
        return 0
    return int(sid.value)


def note_file() -> Path:
    """Where this Windows session's daemon port is noted down."""
    return config_dir() / f"daemon-port.{session_id()}"


def resolve(configured: int) -> int:
    """Port of this session's daemon: the noted one, or the configured one
    if the daemon never had to step aside."""
    try:
        return int(note_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return configured


def announce(port: int, configured: int) -> None:
    """Note the port the daemon has just bound, for the adapter and hooks.

    Back on the configured port the note is removed instead of written:
    every part falls back to it on its own, and a note outliving the
    stranger that caused it would send the hooks to a dead port.

    Deliberately not silent on failure: an unwritable note would leave the
    hooks reporting to a port nobody listens on, and a delivery that is
    never confirmed looks exactly like one that got lost."""
    path = note_file()
    if port == configured:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(port), encoding="utf-8")


def claim_instance() -> bool:
    """Whether this process is THE daemon of its Windows session.

    Adapters come up in bunches - one per Claude session the user opens -
    and every one of them that finds no daemon of ours spawns one. The
    first to get this mutex is the daemon; the others step back before
    they take a microphone, an overlay or a port. Creating a named mutex
    is atomic, so no two of them can both believe they were first, and
    Windows drops the name once the last handle closes - a daemon that
    crashed locks nobody out.

    The handle is kept on the module on purpose: holding it open IS the
    claim. A mutex that cannot be created at all counts as ours - a
    session with two daemons is a smaller failure than one with none."""
    global _instance_handle
    if sys.platform != "win32":
        return True
    # the private copy of the last error is the only one still untouched
    # by the time ctypes hands the call back
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.CreateMutexW(None, True, INSTANCE_MUTEX)
    if handle and ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _instance_handle = handle
    return True
