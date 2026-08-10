"""Tests for the PTT target latch in claude_code_ptt.sessions.

The historical bug: with `effective_pid()` auto-selecting whenever exactly
one session existed, the target moved on its own. Jogi had his main session
pinned implicitly, a JogiLoop worker registered - and the target either went
blank or, once the main session's registration briefly lapsed and the worker
was the only one left, silently landed ON THE WORKER. He then spoke into a
worker instead of his main session.

The rule now: the click is a latch. Nothing but another click moves the
target, except the chosen session disappearing - then the MAIN session (the
oldest still-registered one) takes over. Both halves are about a session,
not a pid: a recycled pid is a stranger, and a session coming back from a
heartbeat lapse keeps both its pin and its seniority.

No GUI, no running daemon, no Win32 window lookup: `find_session_window` is
patched out (it would otherwise spawn the console probe subprocess).
"""
import time
from types import SimpleNamespace

import pytest

from claude_code_ptt import sessions

MAIN = 1000        # Jogi's main session - registers first
WORKER = 2000      # a JogiLoop worker that starts later
WORKER_B = 3000

PROCS: dict[int, int] = {}      # the machine's living pids -> creation time
STAMPS: dict[tuple[int, int], float] = {}    # identity -> 'registered' stamp
STARTED_AGO = 100.0             # how long the test's sessions have been up


@pytest.fixture
def registry(monkeypatch):
    """A registry talking to a fake machine: PROCS is its process list,
    the window lookup is a stub (pid -> fake hwnd). Its voice pool is a
    placeholder - who sounds how is a separate concern
    (tests/test_session_voices.py), the latch only needs the reaper to run
    without asking the real machine for its processes."""
    PROCS.clear()
    STAMPS.clear()
    monkeypatch.setattr(sessions, "find_session_window",
                        lambda pid: pid + 10)
    monkeypatch.setattr(sessions, "process_tree",
                        lambda: {pid: (0, "claude.exe") for pid in PROCS})
    monkeypatch.setattr(sessions, "process_start_time",
                        lambda pid: PROCS.get(pid, 0))
    return sessions.SessionRegistry(SimpleNamespace(retain=lambda pids: None))


def _register(registry, pid, label="proj"):
    """Start the process (if it is not running already) and register it
    with a stamp of its own: in registration order, because the
    main-session rule ranks by it and monotonic() can repeat within one
    Windows tick - and safely in the past, because a session registering
    NOW has to come out younger than the ones already there (otherwise a
    lost rank would still look like the oldest and hide the bug).

    The stamp goes into the rank map too: that is where a session
    returning from a heartbeat lapse gets its original rank back from."""
    PROCS.setdefault(pid, pid)
    info = registry.register(pid, rf"C:\work\{label}")
    identity = (pid, info["started"])
    stamp = STAMPS.setdefault(
        identity, time.monotonic() - STARTED_AGO + len(STAMPS) * 1e-3)
    info["registered"] = stamp
    registry._ranks[identity] = stamp
    return pid


def test_new_session_never_steals_a_manual_target(registry):
    """(a) Jogi picked his main session; workers come and go around it."""
    _register(registry, MAIN, "main")
    registry.select(MAIN)

    _register(registry, WORKER, "card-602")
    assert registry.effective_pid() == MAIN

    _register(registry, WORKER_B, "card-597")
    registry.unregister(WORKER)
    assert registry.effective_pid() == MAIN


def test_manual_target_survives_a_heartbeat_lapse(registry, monkeypatch):
    """The reaper must not silently repoint the target either: a worker
    expiring leaves the pinned main session exactly where it was."""
    _register(registry, MAIN, "main")
    registry.select(MAIN)
    _register(registry, WORKER, "card-602")

    registry._sessions[WORKER]["last_seen"] = (
        time.monotonic() - sessions.HEARTBEAT_TIMEOUT - 1)
    registry._reap()

    assert WORKER not in registry._sessions
    assert registry.effective_pid() == MAIN


def test_target_falls_back_to_the_main_session_when_the_pick_is_gone(registry):
    """(b) The only allowed automatic change: the chosen session ends, so
    the target returns to the main (oldest registered) session."""
    _register(registry, MAIN, "main")
    _register(registry, WORKER, "card-602")
    registry.select(WORKER)
    assert registry.effective_pid() == WORKER

    registry.unregister(WORKER)
    assert registry.effective_pid() == MAIN


def test_expired_pick_also_falls_back_to_the_main_session(registry):
    """Same fallback when the pick dies via the reaper instead of a clean
    unregister (a killed worker never says goodbye)."""
    _register(registry, MAIN, "main")
    _register(registry, WORKER, "card-602")
    registry.select(WORKER)

    registry._sessions[WORKER]["last_seen"] = (
        time.monotonic() - sessions.HEARTBEAT_TIMEOUT - 1)
    registry._reap()

    assert registry.effective_pid() == MAIN


def test_a_returning_session_gets_its_pin_back(registry):
    """The pin sticks to the pid, so an adapter restart (unregister +
    register of the same session) does not cost Jogi his choice."""
    _register(registry, MAIN, "main")
    _register(registry, WORKER, "card-602")
    registry.select(WORKER)

    registry.unregister(WORKER)
    assert registry.effective_pid() == MAIN

    _register(registry, WORKER, "card-602")
    assert registry.effective_pid() == WORKER


def test_a_recycled_pid_does_not_inherit_the_pin(registry):
    """Windows hands pids out again. The worker Jogi pinned ends and some
    later session gets its number - that stranger must NOT become the
    target, because Jogi never clicked it."""
    _register(registry, MAIN, "main")
    _register(registry, WORKER, "card-602")
    registry.select(WORKER)
    assert registry.effective_pid() == WORKER

    del PROCS[WORKER]                          # the worker's process ends
    registry.unregister(WORKER)
    PROCS[WORKER] = WORKER + 1                 # a stranger gets the number
    _register(registry, WORKER, "card-588")

    assert registry.effective_pid() == MAIN


def test_the_main_session_keeps_its_rank_after_a_heartbeat_lapse(registry):
    """A lapse drops the entry outright and the adapter re-registers
    seconds later. That must not cost the main session its seniority: it
    is the fallback target, and the only other candidate here is a
    worker."""
    _register(registry, MAIN, "main")
    _register(registry, WORKER, "card-602")

    registry._sessions[MAIN]["last_seen"] = (
        time.monotonic() - sessions.HEARTBEAT_TIMEOUT - 1)
    registry._reap()
    assert MAIN not in registry._sessions
    assert registry.effective_pid() == WORKER  # the only one left, briefly

    registry.register(MAIN, r"C:\work\main")   # adapter re-registers
    assert registry.effective_pid() == MAIN


def test_a_dead_session_does_not_keep_its_rank_forever(registry):
    """The rank map outlives entries, so it needs a floor: once the
    process is really gone, its rank goes with it (and cannot be inherited
    by whoever gets the pid next)."""
    _register(registry, MAIN, "main")
    assert registry._ranks

    del PROCS[MAIN]
    registry.unregister(MAIN)
    registry._reap()

    assert registry._ranks == {}


def test_re_register_does_not_change_who_is_the_main_session(registry):
    """A re-registering main session must stay the main session - its
    first registration counts, not the latest one."""
    _register(registry, MAIN, "main")
    _register(registry, WORKER, "card-602")

    registry.register(MAIN, r"C:\work\main")      # heartbeat lapse -> re-add
    assert registry.effective_pid() == MAIN


def test_automatic_mode_still_follows_the_session_list(registry):
    """(c) Nothing clicked = automatic mode: the target is not frozen, it
    follows the registry on its own (the main session). Focus tracking
    itself was removed from the daemon in e608548, so this fallback IS the
    automatic mode - and a manual pick is what turns it off."""
    assert registry.effective_pid() == 0           # no sessions yet

    _register(registry, MAIN, "main")
    assert registry.effective_pid() == MAIN

    _register(registry, WORKER, "card-602")
    assert registry.effective_pid() == MAIN        # worker does not win

    registry.unregister(MAIN)
    assert registry.effective_pid() == WORKER      # automatic: moves along

    registry.select(WORKER_B)                      # a pick ends automatic
    _register(registry, WORKER_B, "card-597")
    registry.unregister(WORKER_B)
    registry.select(0)                             # back to automatic
    assert registry.effective_pid() == WORKER
