"""Event-intake subscriber tests (task 2.1), from specs/event-intake.

The subscriber is driven through a fake ``EventStream`` (no live ntfy, no
websocket lib) exactly as the Signal adapter is driven through ``FakeBridge``.
Covered: receive+convert, control-frame skipping, last-seen-id tracking so
reconnect resumes with ``since`` (exactly-once), backoff on transport error,
and that a persistently-unreachable topic never crashes the loop.
"""

from __future__ import annotations

from typing import AsyncIterator

from henk.events.intake import (
    RETENTION_REPLAY_SINCE,
    EventIntake,
    EventStream,
    EventStreamError,
)


class FakeStream:
    """Yields scripted ntfy frames per (re)connection; records ``since`` args.

    ``scripts`` is a list of per-connection scripts. Each script is a list whose
    items are either dicts (frames to yield) or Exception instances (raised at
    that point). Each call to ``subscribe`` consumes the next script; the last
    script repeats so a quiet steady state doesn't exhaust the fake.
    """

    def __init__(self, scripts: list[list]) -> None:
        self._scripts = scripts
        self.since_calls: list[str | None] = []
        self._n = 0

    async def subscribe(self, since: str | None) -> AsyncIterator[dict]:
        self.since_calls.append(since)
        script = self._scripts[min(self._n, len(self._scripts) - 1)]
        self._n += 1
        for item in script:
            if isinstance(item, Exception):
                raise item
            yield item


async def _no_sleep(_d: float) -> None:
    """Backoff is not under test here — keep the reconnect path instant."""
    return None


def _msg(mid: str, title: str, message: str = "") -> dict:
    return {"id": mid, "event": "message", "topic": "henk-events",
            "title": title, "message": message}


async def _collect(intake: EventIntake, limit: int) -> list:
    out = []
    async for ev in intake.events():
        out.append(ev)
        if len(out) >= limit:
            break
    return out


async def test_message_received_and_converted():
    stream = FakeStream([[_msg("a1", "Gatus: test/x", "triggered")]])
    intake = EventIntake(stream)
    events = await _collect(intake, 1)
    assert events[0].id == "a1"
    assert events[0].title == "Gatus: test/x"
    assert events[0].arrival_time > 0  # stamped on arrival, not from the sensor


async def test_control_frames_skipped():
    stream = FakeStream([[
        {"event": "open", "topic": "henk-events"},
        {"event": "keepalive", "topic": "henk-events"},
        _msg("a1", "real"),
    ]])
    intake = EventIntake(stream)
    events = await _collect(intake, 1)
    assert events[0].title == "real"  # open/keepalive did not yield events


async def test_reconnect_resumes_from_last_seen_id():
    # First connection yields A then drops; reconnect must pass since="a1".
    stream = FakeStream([
        [_msg("a1", "first"), EventStreamError("dropped")],
        [_msg("b2", "second")],
    ])
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    intake = EventIntake(stream, sleep=fake_sleep)
    events = await _collect(intake, 2)
    assert [e.title for e in events] == ["first", "second"]
    assert stream.since_calls[0] is None       # cold start: no cursor
    assert stream.since_calls[1] == "a1"        # resume after the last-seen id
    assert slept, "should back off before reconnecting"


async def test_event_during_disconnection_processed_once():
    # Down while one event is published; reconnect within retention → seen once.
    stream = FakeStream([
        [EventStreamError("down")],       # cold connection fails immediately
        [_msg("a1", "the-only-event")],   # reconnect delivers the event
    ])

    async def fake_sleep(d: float) -> None:
        return None

    intake = EventIntake(stream, sleep=fake_sleep)
    events = await _collect(intake, 1)
    assert [e.title for e in events] == ["the-only-event"]


async def test_initial_offset_seeds_first_subscribe_since():
    # A restart seeds intake from the persisted checkpoint so the first
    # subscribe resumes with since=<offset> (event published while stopped).
    stream = FakeStream([[_msg("z9", "after-restart")]])
    intake = EventIntake(stream, initial_offset="a1")
    events = await _collect(intake, 1)
    assert events[0].title == "after-restart"
    assert stream.since_calls[0] == "a1"  # resumed from the persisted checkpoint


async def test_no_initial_offset_cold_starts_without_since():
    # First ever start: no checkpoint → subscribe with no since.
    stream = FakeStream([[_msg("a1", "first")]])
    intake = EventIntake(stream)  # no initial_offset
    await _collect(intake, 1)
    assert stream.since_calls[0] is None


async def test_backlog_after_downtime_replays_from_seeded_offset():
    # Events published while stopped accumulate; on restart the seeded offset
    # replays the whole since-gated backlog (debounce collapses it downstream).
    stream = FakeStream([[
        _msg("b1", "svc/a"), _msg("b2", "svc/b"), _msg("b3", "svc/c"),
    ]])
    intake = EventIntake(stream, initial_offset="a0")
    events = await _collect(intake, 3)
    assert [e.title for e in events] == ["svc/a", "svc/b", "svc/c"]
    assert stream.since_calls[0] == "a0"  # resumed from checkpoint, not cold


async def test_persistent_failure_backs_off_without_crashing():
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)
        if len(slept) >= 4:
            raise _StopLoop()  # break the otherwise-infinite retry for the test

    class _StopLoop(Exception):
        pass

    stream = FakeStream([[EventStreamError("nope")]])
    intake = EventIntake(stream, sleep=fake_sleep, backoff_base=1.0, max_backoff=8.0)
    try:
        await _collect(intake, 1)
    except _StopLoop:
        pass
    # Backoff grows and is capped — never a tight spin.
    assert slept[:4] == [1.0, 2.0, 4.0, 8.0]


# --- Unresumable-checkpoint recovery (deploy-verify item 5, 2026-07-24) -------
#
# Live probe against the vps ntfy established the `since` contract:
#   * exactly 12 base62 chars  -> parsed as a message id  -> HTTP 200
#   * an id no longer cached   -> HTTP 200 + the full retained cache (NOT an error)
#   * anything else            -> HTTP 400
# So eviction is benign, but a MALFORMED checkpoint is a hard rejection, and the
# bare backoff loop would retry the same bad `since` forever — silently killing
# intake, the exact failure class this change exists to eliminate.


async def test_since_rejection_falls_back_to_full_retention_replay():
    # 400 on the seeded offset, then a healthy connection.
    stream = FakeStream([
        [EventStreamError("bad since", status=400)],
        [_msg("n1", "svc/a")],
    ])
    intake = EventIntake(stream, initial_offset="bogus-checkpoint")
    events = await _collect(intake, 1)

    assert [e.title for e in events] == ["svc/a"]
    # Retried with the replay-all sentinel, NOT the rejected value and NOT a cold
    # subscribe (which would silently drop everything published while stopped).
    assert stream.since_calls == ["bogus-checkpoint", RETENTION_REPLAY_SINCE]


async def test_since_rejection_notifies_the_owner():
    notices: list[str] = []

    async def on_rejected() -> None:
        notices.append("sent")

    stream = FakeStream([
        [EventStreamError("bad since", status=400)],
        [_msg("n1", "svc/a")],
    ])
    intake = EventIntake(
        stream, initial_offset="bogus", on_since_rejected=on_rejected
    )
    await _collect(intake, 1)
    # Unattended agent: a wedge that only a human can clear must not be silent.
    assert notices == ["sent"]


async def test_recovers_a_real_offset_after_the_fallback():
    # Self-healing: once events flow again the sentinel is replaced by a real id,
    # so a later reconnect resumes normally instead of re-replaying the cache.
    stream = FakeStream([
        [EventStreamError("bad since", status=400)],
        [_msg("n1", "svc/a"), EventStreamError("dropped")],
        [_msg("n2", "svc/b")],
    ])
    intake = EventIntake(stream, initial_offset="bogus", sleep=_no_sleep)
    events = await _collect(intake, 2)

    assert [e.title for e in events] == ["svc/a", "svc/b"]
    assert stream.since_calls == ["bogus", RETENTION_REPLAY_SINCE, "n1"]


async def test_transient_error_does_not_discard_the_offset():
    # Regression guard: only a since-REJECTION resets the cursor. An ordinary
    # transport blip must keep resuming from the last-seen id, or every network
    # hiccup would trigger a full-cache replay storm.
    stream = FakeStream([
        [_msg("n1", "svc/a"), EventStreamError("connection reset")],
        [_msg("n2", "svc/b")],
    ])
    intake = EventIntake(stream, sleep=_no_sleep)
    events = await _collect(intake, 2)

    assert [e.title for e in events] == ["svc/a", "svc/b"]
    assert stream.since_calls == [None, "n1"]


async def test_rejected_sentinel_does_not_spin():
    # If even the sentinel is rejected there is nothing left to fall back to:
    # take the normal backoff path rather than looping with no delay.
    slept: list[float] = []

    class _StopLoop(Exception):
        pass

    async def fake_sleep(d: float) -> None:
        slept.append(d)
        if len(slept) >= 3:
            raise _StopLoop()

    stream = FakeStream([[EventStreamError("bad since", status=400)]])
    intake = EventIntake(
        stream, initial_offset="bogus", sleep=fake_sleep, backoff_base=1.0
    )
    try:
        await _collect(intake, 1)
    except _StopLoop:
        pass
    assert slept[:3] == [1.0, 2.0, 4.0]


async def test_cold_subscribe_rejection_is_not_treated_as_a_bad_checkpoint():
    # No `since` was sent, so a 400 cannot be about the checkpoint — don't
    # "recover" by replaying the cache, and don't notify.
    notices: list[str] = []

    async def on_rejected() -> None:
        notices.append("sent")

    stream = FakeStream([
        [EventStreamError("bad request", status=400)],
        [_msg("n1", "svc/a")],
    ])
    intake = EventIntake(stream, on_since_rejected=on_rejected, sleep=_no_sleep)
    await _collect(intake, 1)

    assert stream.since_calls == [None, None]
    assert notices == []
