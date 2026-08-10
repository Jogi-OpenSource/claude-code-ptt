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
parts keep finding each other.

A machine with a single logged-on account never writes the note and stays
on the configured port.
"""
import ctypes
import socket
import sys
from pathlib import Path

from .config import config_dir

PORT_SCAN = 20                             # ports tried from the configured one


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
