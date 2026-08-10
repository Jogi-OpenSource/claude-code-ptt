"""Which localhost port the daemon of THIS Windows session listens on.

Loopback belongs to the whole machine, the desktop does not: with a second
account logged on, its daemon answers on the configured port just fine,
while its overlay sits on a desktop nobody here can see and its log is
written into that account's profile. Attaching to it leaves this session
mute. The adapter therefore asks the daemon which Windows session it runs
in (see the /status endpoint), steps aside to a free port when the answer
is a stranger's, and notes that port down here - beside the config, but
per Windows session, because two sessions of the same account share the
config file. The daemon and both hooks read the same note, so all four
parts keep finding each other. Stepping aside is serialised per session
(see start_lock): adapters start in bunches, and each of them stepping
aside on its own would leave one daemon per adapter.

A machine with a single logged-on account never writes the note and stays
on the configured port.
"""
import contextlib
import ctypes
import socket
import sys
from pathlib import Path

from .config import config_dir

PORT_SCAN = 20                             # ports tried from the configured one
START_LOCK = "Local\\claude-code-ptt-daemon"   # Local\ = this Windows session
START_LOCK_TIMEOUT = 60.0


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
    if the adapter never had to step aside."""
    try:
        return int(note_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return configured


def remember(port: int) -> None:
    """Note the port for the daemon and the hooks of this session.

    Deliberately not silent on failure: an unwritable note would leave the
    hooks reporting to a port nobody listens on, and a delivery that is
    never confirmed looks exactly like one that got lost."""
    path = note_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(port), encoding="utf-8")


def in_use(port: int) -> bool:
    """Whether somebody else already owns that localhost port.

    Asked by binding, not by connecting: a connect leaves an unanswered
    connection in the listener's backlog, and a second probe against a
    full backlog is refused - which reads as "free" and would send our
    daemon straight into a port collision (measured on Windows)."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def free_port(configured: int) -> int:
    """First port from the configured one upwards that nobody owns.

    Starting at the configured port on every lookup, not above the noted
    one: once the stranger is gone, the next start returns to the default
    instead of drifting further up with every collision."""
    for port in range(configured, configured + PORT_SCAN):
        if not in_use(port):
            return port
    raise RuntimeError(f"no free daemon port near {configured}")


def claim(configured: int) -> int:
    """Pick the port this session's daemon gets and leave the note behind
    that the daemon and both hooks follow.

    Back on the configured port the note is removed instead of written:
    every part falls back to it on its own, and a note outliving the
    stranger that caused it would send the hooks to a dead port.

    Only meaningful under start_lock() - between finding a port free and
    the daemon actually binding it lies that daemon's whole startup."""
    port = free_port(configured)
    if port == configured:
        note_file().unlink(missing_ok=True)
    else:
        remember(port)
    return port


@contextlib.contextmanager
def start_lock():
    """Let only one adapter of this Windows session start a daemon.

    Adapters come up in bunches - one per Claude session the user opens -
    and a starting daemon needs seconds before it binds its port. Without
    this, each of them looks, finds nothing of ours listening yet, claims
    a port of its own and spawns another daemon, the last one overwriting
    the note. The lock is therefore held across the whole look-claim-
    spawn-wait, not just the claim.

    Windows releases a mutex when its owner dies, so a killed adapter
    cannot lock the next one out. Waiting in vain runs on regardless: a
    second daemon is a smaller failure than an adapter that never speaks
    for its session again."""
    if sys.platform != "win32":
        yield
        return
    kernel32 = ctypes.windll.kernel32
    # handles are pointers; without a restype ctypes truncates them to int
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.CreateMutexW(None, False, START_LOCK)
    if not handle:
        yield
        return
    # WAIT_OBJECT_0, or WAIT_ABANDONED for a mutex whose owner died
    owned = kernel32.WaitForSingleObject(
        handle, int(START_LOCK_TIMEOUT * 1000)) in (0x0, 0x80)
    try:
        yield
    finally:
        if owned:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)
