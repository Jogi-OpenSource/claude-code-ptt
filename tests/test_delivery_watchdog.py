"""When a pending send counts as lost (daemon._send_expired).

A text that never reached the session's queue must not keep the overlay on
SENDE for as long as the session stays busy - that blocked every following
message's status for up to an hour.
"""
from claude_code_ptt.daemon import (CONFIRM_TIMEOUT, QUEUE_GRACE, QUEUE_MAX,
                                    UNQUEUED_BUSY_MAX, _send_expired)


def _send(started: float, queued: bool = False) -> dict:
    return {"text": "[mic] hallo", "pid": 1, "started": started,
            "deadline": started + CONFIRM_TIMEOUT, "queued": queued,
            "idle_since": None, "transcript": ""}


def test_unqueued_send_within_the_confirm_window_is_kept():
    assert not _send_expired(_send(0.0), now=CONFIRM_TIMEOUT - 1, busy=False)


def test_unqueued_send_past_the_window_on_an_idle_session_is_lost():
    assert _send_expired(_send(0.0), now=CONFIRM_TIMEOUT + 1, busy=False)


def test_unqueued_send_on_a_busy_session_gets_a_short_extension():
    assert not _send_expired(_send(0.0), now=UNQUEUED_BUSY_MAX - 1, busy=True)


def test_unqueued_send_on_a_busy_session_is_lost_after_the_extension():
    # the bug: this stayed "sending" until QUEUE_MAX (one hour)
    assert _send_expired(_send(0.0), now=UNQUEUED_BUSY_MAX + 1, busy=True)


def test_queued_send_survives_while_its_session_works():
    assert not _send_expired(_send(0.0, queued=True), now=QUEUE_MAX - 1, busy=True)


def test_queued_send_is_lost_after_the_grace_once_the_session_idles():
    send = _send(0.0, queued=True)
    assert not _send_expired(send, now=100.0, busy=False)
    assert _send_expired(send, now=100.0 + QUEUE_GRACE + 1, busy=False)
