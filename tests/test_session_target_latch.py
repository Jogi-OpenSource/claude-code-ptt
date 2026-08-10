"""Tests for the PTT target latch in claude_code_ptt.sessions.

The historical bug: with `effective_pid()` auto-selecting whenever exactly
one session existed, the target moved on its own. Jogi had his main session
pinned implicitly, a JogiLoop worker registered - and the target either went
blank or, once the main session's registration briefly lapsed and the worker
was the only one left, silently landed ON THE WORKER. He then spoke into a
worker instead of his main session.

The rule now: the click is a latch. Nothing but another click moves the
target, except the chosen session disappearing - then the MAIN session (the
oldest still-registered one) takes over.

No GUI, no running daemon, no Win32 window lookup: `find_session_window` is
patched out (it would otherwise spawn the console probe subprocess).
"""
import time

import pytest

from claude_code_ptt import sessions

MAIN = 1000        # Jogi's main session - registers first
WORKER = 2000      # a JogiLoop worker that starts later
WORKER_B = 3000


@pytest.fixture
def registry(monkeypatch):
    """A registry whose window lookup is a stub (pid -> fake hwnd)."""
    monkeypatch.setattr(sessions, "find_session_window",
                        lambda pid: pid + 10)
    return sessions.SessionRegistry()


def _register(registry, pid, label="proj"):
    """Register with a distinct 'registered' stamp - the main-session rule
    ranks by it, and monotonic() can repeat within one tick on Windows."""
    registry.register(pid, rf"C:\work\{label}")
    registry._sessions[pid]["registered"] = time.monotonic() + pid * 1e-3
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
    registry._reap_once()

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
    registry._reap_once()

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
