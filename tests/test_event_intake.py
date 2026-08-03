"""Event-intake subscriber tests (task 2.1), from specs/event-intake.

The subscriber is driven through a fake ``EventStream`` (no live ntfy, no
websocket lib) exactly as the Signal adapter is driven through ``FakeBridge``.
Covered: receive+convert, control-frame skipping, last-seen-id tracking so
reconnect resumes with ``since`` (exactly-once), backoff on transport error,
and that a persistently-unreachable topic never crashes the loop.

The liveness watchdog (intake-liveness-watchdog) is driven, never elapsed: see
the harness note above ``FakeMono``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, NamedTuple

from henk.events.intake import (
    LIVENESS_TRIP_MARKER,
    RETENTION_REPLAY_SINCE,
    EventIntake,
    EventStream,
    EventStreamError,
)


class FakeMono:
    """A monotonic clock advanced by the *script*, not by the event loop.

    ``asyncio.timeout`` reads the loop clock, which ``EventIntake``'s injected
    ``clock``/``sleep`` seams cannot move, and ``FakeStream`` yields every
    scripted frame back-to-back with no awaits — so a test phrased "well past the
    deadline" would pass in microseconds while proving nothing. Wire time is
    therefore simulated: an :class:`Advance` script item moves this clock, and
    :class:`DrivenTimeout` reads it. ``mono_clock`` and ``timeout_ctx`` are
    substituted **as a pair**; a fake clock with a real ``asyncio.timeout`` is
    incoherent.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Advance:
    """Script item: simulate ``seconds`` of wire time before the next frame."""

    __slots__ = ("seconds",)

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


class DrivenTimeout:
    """Stand-in for ``asyncio.timeout``, driven by :class:`FakeMono`.

    Records every budget it is handed so assertions can target the arithmetic
    rather than which context manager ran, and raises ``TimeoutError`` — never
    ``CancelledError``, or intake's normalisation of the trip is never exercised.
    It fires in two places, mirroring the real thing:

    * on entry when the budget is already non-positive (measured on the real
      ``asyncio.timeout``: ``-5.0``, ``-0.001`` and ``0.0`` all raise at 0.000s,
      with no ``ValueError``, no clamp and no log);
    * on exit when the body consumed more simulated time than its budget allowed
      — including when the body ended the stream, because a clean end reached
      after the deadline would in reality have been pre-empted by the trip.

    It does **not** cancel anything, so it tests the budget arithmetic and never
    the cancellation. The probes under ``henk-probes/`` cover that half.
    """

    def __init__(self, clock: FakeMono) -> None:
        self._clock = clock
        self.budgets: list[float] = []

    def __call__(self, budget: float) -> "_DrivenScope":
        self.budgets.append(budget)
        return _DrivenScope(self._clock, budget)


class _DrivenScope:
    def __init__(self, clock: FakeMono, budget: float) -> None:
        self._clock = clock
        self._budget = budget
        self._entered_at = 0.0

    async def __aenter__(self) -> "_DrivenScope":
        if self._budget <= 0:
            raise TimeoutError
        self._entered_at = self._clock()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        overran = self._clock() - self._entered_at > self._budget
        if overran and exc_type in (None, StopAsyncIteration):
            raise TimeoutError
        return False


class FakeStream:
    """Yields scripted ntfy frames per (re)connection; records ``since`` args.

    ``scripts`` is a list of per-connection scripts. Each script is a list whose
    items are either dicts (frames to yield), :class:`Advance` markers (simulated
    wire time, requires ``clock``) or Exception instances (raised at that point).
    Each call to ``subscribe`` consumes the next script; the last script repeats
    so a quiet steady state doesn't exhaust the fake.
    """

    def __init__(self, scripts: list[list], clock: FakeMono | None = None) -> None:
        self._scripts = scripts
        self._clock = clock
        self.since_calls: list[str | None] = []
        self._n = 0

    async def subscribe(self, since: str | None) -> AsyncIterator[dict]:
        self.since_calls.append(since)
        script = self._scripts[min(self._n, len(self._scripts) - 1)]
        self._n += 1
        for item in script:
            if isinstance(item, Advance):
                assert self._clock is not None, "Advance needs a FakeMono clock"
                self._clock.advance(item.seconds)
                continue
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


class _StopLoop(Exception):
    """Escape hatch out of the otherwise-endless reconnect loop, from the sleep."""


def _recording_sleep(slept: list[float], *, stop_after: int | None = None):
    async def _sleep(delay: float) -> None:
        slept.append(delay)
        if stop_after is not None and len(slept) >= stop_after:
            raise _StopLoop()

    return _sleep


class Harness(NamedTuple):
    intake: EventIntake
    stream: FakeStream
    clock: FakeMono
    timeouts: DrivenTimeout


def _harness(
    scripts: list[list],
    *,
    deadline: float = 135.0,
    sleep=None,
    **kwargs,
) -> Harness:
    """Build an intake whose liveness budget is driven by a simulated clock."""
    clock = FakeMono()
    timeouts = DrivenTimeout(clock)
    stream = FakeStream(scripts, clock=clock)
    intake = EventIntake(
        stream,
        mono_clock=clock,
        timeout_ctx=timeouts,
        liveness_deadline=deadline,
        sleep=_no_sleep if sleep is None else sleep,
        **kwargs,
    )
    return Harness(intake, stream, clock, timeouts)


async def _drain(intake: EventIntake, *, limit: int | None = None) -> list:
    """Consume ``events()`` until ``limit`` events or the sleep stops the loop.

    Closes the intake generator explicitly rather than only ``break``ing: the
    broad ``finally`` inside ``events()`` fires when the *outer* generator is
    closed, so a driver that merely abandons it measures ``shutdown_asyncgens``
    timing instead of the code under test.
    """
    out: list = []
    agen = intake.events()
    try:
        async for ev in agen:
            out.append(ev)
            if limit is not None and len(out) >= limit:
                break
    except _StopLoop:
        pass
    finally:
        await agen.aclose()
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


class RejectingStream:
    """400s every real id, serves events on the sentinel — a flapping cursor.

    Models the case D8's prose did not consider: not a one-off bad checkpoint,
    but a resume point that keeps being rejected after each recovery.
    """

    def __init__(self, per_cycle: int = 1) -> None:
        self.since_calls: list[str | None] = []
        self._n = 0
        self._per_cycle = per_cycle

    async def subscribe(self, since: str | None) -> AsyncIterator[dict]:
        self.since_calls.append(since)
        if since != RETENTION_REPLAY_SINCE:
            raise EventStreamError("bad since", status=400)
        self._n += 1
        for i in range(self._per_cycle):
            yield _msg(f"r{self._n}-{i}", "svc/replayed")


async def test_repeated_rejection_notifies_only_once():
    # The notice is documented as one-shot and must behave like the core's
    # durability latch: an unattended agent that DMs the owner on every cycle
    # has inverted the purpose of the alert.
    notices: list[str] = []

    async def on_rejected() -> None:
        notices.append("sent")

    stream = RejectingStream()
    intake = EventIntake(
        stream, initial_offset="realid000001",
        on_since_rejected=on_rejected, sleep=_no_sleep,
    )
    await _collect(intake, 4)
    assert notices == ["sent"]


async def test_repeated_rejection_backs_off_after_the_first_recovery():
    # First recovery reconnects immediately (the sentinel is known-valid).
    # Later ones must be paced, or a flapping cursor re-downloads the whole
    # 72h cache in a tight loop and rewrites a suppression record per event.
    # Two independent discriminators, and both are now load-bearing: the
    # interleaved trace (a sleep sits between a rejection and its retry), and the
    # delay VALUES. Since the liveness change, this stream's clean end advances the
    # penalty and the replayed message zeroes it again, so the delays alternate
    # 1.0 (clean end, penalty 0) / 2.0 (paced recovery, penalty 1) -- measured -
    # rather than being a flat backoff_base that says nothing about which path ran.
    trace: list[str] = []
    delays: list[float] = []

    async def rec_sleep(d: float) -> None:
        trace.append("sleep")
        delays.append(d)

    class TracingStream(RejectingStream):
        async def subscribe(self, since: str | None) -> AsyncIterator[dict]:
            trace.append(f"sub:{since}")
            async for frame in super().subscribe(since):
                yield frame

    stream = TracingStream()
    intake = EventIntake(
        stream, initial_offset="realid000001", sleep=rec_sleep, backoff_base=1.0
    )
    await _collect(intake, 4)

    # First rejection reconnects immediately: nothing between it and the sentinel.
    assert trace[0] == "sub:realid000001"
    assert trace[1] == f"sub:{RETENTION_REPLAY_SINCE}"
    # A later rejection must be paced -- a sleep sits between it and its retry.
    second = trace.index("sub:r1-0")
    nxt = trace.index(f"sub:{RETENTION_REPLAY_SINCE}", second)
    assert "sleep" in trace[second:nxt], f"repeat recovery not paced; {trace}"
    # The paced recovery costs more than the clean end that preceded it.
    assert delays[:4] == [1.0, 2.0, 1.0, 2.0], delays


async def test_first_recovery_reconnects_without_sleeping():
    # Pins D8's "the sentinel is valid, so no backoff is needed" decision --
    # deleting the `continue` must fail a test, not just run slower.
    slept: list[float] = []

    async def rec_sleep(d: float) -> None:
        slept.append(d)

    stream = FakeStream([
        [EventStreamError("bad since", status=400)],
        [_msg("n1", "svc/a")],
    ])
    intake = EventIntake(stream, initial_offset="bogus", sleep=rec_sleep)
    await _collect(intake, 1)
    assert slept == []


async def test_non_400_status_is_not_a_since_rejection():
    # Pins the predicate itself: only a 400 means "your resume point is bad".
    # A 500/403 must keep the cursor -- resetting it on a server blip or a
    # revoked token would trigger a pointless full replay and a false alert.
    notices: list[str] = []

    async def on_rejected() -> None:
        notices.append("sent")

    for status in (403, 500, 502):
        stream = FakeStream([
            [EventStreamError("server said no", status=status)],
            [_msg("n1", "svc/a")],
        ])
        intake = EventIntake(
            stream, initial_offset="realid000001",
            on_since_rejected=on_rejected, sleep=_no_sleep,
        )
        await _collect(intake, 1)
        assert stream.since_calls == ["realid000001", "realid000001"], status
    assert notices == []


async def test_notice_failure_does_not_kill_intake():
    # The notice is best-effort: a Signal outage must not take intake with it.
    async def boom() -> None:
        raise RuntimeError("signal down")

    stream = FakeStream([
        [EventStreamError("bad since", status=400)],
        [_msg("n1", "svc/a")],
    ])
    intake = EventIntake(
        stream, initial_offset="bogus", on_since_rejected=boom, sleep=_no_sleep
    )
    events = await _collect(intake, 1)
    assert [e.title for e in events] == ["svc/a"]


# --- Liveness watchdog (intake-liveness-watchdog, specs/event-intake) ---------
#
# A proof-of-life frame is any frame whose `event` is not `open`. The budget is
# `deadline - (now - last_proof_of_life)`, re-established immediately before each
# subscribe (after the backoff sleep) and advanced for a delivered event AFTER its
# `yield` returns. Each test below is written so that the wrong implementation
# fails it, not merely so that the right one passes.

_OPEN = {"event": "open", "topic": "henk-events"}
_KEEPALIVE = {"event": "keepalive", "topic": "henk-events"}


async def test_silent_stream_is_abandoned_and_resumes_from_last_seen_id():
    # The first connection delivers nothing at all -- so `_collect` would never
    # return -- and goes quiet past the budget. The reconnect must resume from the
    # cursor, not cold, or every trip would silently drop the backlog.
    h = _harness([[Advance(200.0)], [_msg("b2", "after")]], initial_offset="a0")
    events = await _drain(h.intake, limit=1)

    assert [e.title for e in events] == ["after"]
    assert h.stream.since_calls == ["a0", "a0"]
    assert h.timeouts.budgets[0] == 135.0  # a full window on the first subscribe


async def test_keepalives_alone_keep_a_quiet_subscription_healthy():
    # The sole spec-level bound on the top risk (a flapping watchdog on a healthy
    # but event-free tailnet), so it asserts the ABSENCE of a trip across gaps the
    # test explicitly caused -- not merely that nothing happened.
    script = [
        _KEEPALIVE, Advance(50.0), _KEEPALIVE, Advance(50.0), _KEEPALIVE,
        Advance(50.0), _msg("a1", "finally"),
    ]
    h = _harness([script])
    started = h.clock.now
    events = await _drain(h.intake, limit=1)

    assert [e.title for e in events] == ["finally"]
    assert h.clock.now - started > 135.0, "the test never reached the deadline"
    assert h.stream.since_calls == [None], "a healthy quiet stream reconnected"
    assert h.intake.liveness_state()["backoff_penalty"] == 0
    # Every keepalive re-armed the full window; none of them shrank it.
    assert h.timeouts.budgets == [135.0, 135.0, 135.0, 135.0]


async def test_open_flood_still_trips_and_each_connection_gets_a_full_window():
    # The case that fails under a full-window-per-retrieval budget: `open` frames
    # arriving more often than the deadline keep restarting it, so it measurably
    # never fires (40 open frames, no trip). Under the remaining-budget form the
    # window SHRINKS across them.
    flood = [Advance(50.0), _OPEN, Advance(50.0), _OPEN, Advance(50.0), _OPEN]
    h = _harness([flood, flood, [_msg("z1", "after")]])
    events = await _drain(h.intake, limit=1)

    assert [e.title for e in events] == ["after"]
    # Two flooded connections, each starting from a full window (the trailing 135.0
    # is the delivering connection's own first retrieval): a budget established
    # once outside the reconnect loop would hand connection 2 an expired one.
    assert h.timeouts.budgets == [135.0, 85.0, 35.0, 135.0, 85.0, 35.0, 135.0]
    assert h.stream.since_calls == [None, None, None]


async def test_open_then_silence_trips_just_past_the_deadline():
    # Bracketing the deadline is what separates "trips at the right time" from
    # "trips eventually" -- the latter cannot distinguish a correct implementation
    # from one keyed on any frame.
    inside = _harness([[_OPEN, Advance(130.0), _msg("a1", "inside")]])
    assert [e.title for e in await _drain(inside.intake, limit=1)] == ["inside"]
    assert inside.stream.since_calls == [None], "tripped inside the window"

    past = _harness([
        [_OPEN, Advance(140.0), _msg("a1", "lost")],
        [_msg("b2", "after")],
    ])
    assert [e.title for e in await _drain(past.intake, limit=1)] == ["after"]
    # The late frame is discarded with the connection, exactly as a real timeout
    # cancelling the read would -- so the resume is still cold.
    assert past.stream.since_calls == [None, None]


async def test_open_then_eof_escalates_and_the_timestamp_goes_stale():
    # `open` is not proof of life, so nothing resets the penalty: the delays must
    # escalate rather than spin at a fixed 1.0s forever. This is what makes D4's
    # collision unreintroducible.
    slept: list[float] = []
    h = _harness([[_OPEN]], sleep=_recording_sleep(slept, stop_after=4))
    before = h.intake.liveness_state()["last_proof_of_life_at"]
    await _drain(h.intake)

    assert slept == [1.0, 2.0, 4.0, 8.0]
    state = h.intake.liveness_state()
    assert state["last_proof_of_life_at"] == before, "open advanced the timestamp"
    assert state["backoff_penalty"] == 4


async def test_clean_end_after_a_healthy_period_costs_only_the_base_delay():
    slept: list[float] = []
    h = _harness([[_msg("a1", "healthy")]], sleep=_recording_sleep(slept, stop_after=1))
    await _drain(h.intake)
    assert slept == [1.0]


async def test_clean_end_then_error_advances_the_penalty():
    # The most surprising half of D4, asserted AS INTENDED: the penalty counter now
    # advances on a clean end where today it does not, so an ntfy restart (a clean
    # end followed by connect failures) costs 2.0s on the next failure, not 1.0s.
    slept: list[float] = []
    h = _harness(
        [[_msg("a1", "healthy")], [EventStreamError("boom")]],
        sleep=_recording_sleep(slept, stop_after=2),
    )
    await _drain(h.intake)
    assert slept == [1.0, 2.0]


async def test_a_liveness_trip_does_not_kill_intake():
    # Distinct from the reconnect assertion: without this, the failure mode is a
    # permanently hung consumer and no log line -- strictly worse than the bug
    # being fixed, because `TimeoutError` is not an `EventStreamError`.
    h = _harness([
        [_msg("a1", "before"), Advance(200.0)],
        [_msg("b2", "after")],
    ])
    events = await _drain(h.intake, limit=2)
    assert [e.title for e in events] == ["before", "after"]


async def test_control_frame_id_is_never_used_as_a_resume_point():
    # ntfy control frames carry an `id`. Writing one into the cursor is either
    # 400ed (full retention replay + an owner DM) or accepted, silently skipping
    # every message published since. No existing test guards this.
    ctl = [
        {"event": "open", "id": "ctl1", "topic": "henk-events"},
        {"event": "keepalive", "id": "ctl2", "topic": "henk-events"},
        EventStreamError("dropped"),
    ]
    cold = _harness([ctl, [_msg("m1", "real")]])
    await _drain(cold.intake, limit=1)
    assert cold.stream.since_calls == [None, None]  # no message yet -> still cold

    seeded = _harness([ctl, [_msg("m1", "real")]], initial_offset="a0")
    await _drain(seeded.intake, limit=1)
    assert seeded.stream.since_calls == ["a0", "a0"]  # last MESSAGE id, not ctl2


async def test_consumer_latency_does_not_trip_the_watchdog():
    # The deliberately slow consumer pins two things at once: the timeout scope
    # excludes the consumer (a wide scope raises CancelledError into it), and
    # consumer time is not charged against the budget (a stale anchor trips).
    script = [
        _msg("a1", "one"), Advance(10.0), _msg("a2", "two"),
        Advance(10.0), _msg("a3", "three"),
    ]
    h = _harness([script])
    out: list = []
    agen = h.intake.events()
    try:
        async for ev in agen:
            out.append(ev)
            h.clock.advance(200.0)  # slower than the whole deadline
            if len(out) >= 3:
                break
    finally:
        await agen.aclose()

    assert [e.title for e in out] == ["one", "two", "three"]
    assert h.stream.since_calls == [None], "consumer latency caused a reconnect"
    assert h.intake.liveness_state()["backoff_penalty"] == 0


class CountingStream(FakeStream):
    """Counts connections opened vs. per-connection generators finalised."""

    def __init__(self, scripts: list[list], clock: FakeMono | None = None) -> None:
        super().__init__(scripts, clock=clock)
        self.opened = 0
        self.finalised = 0

    async def subscribe(self, since: str | None) -> AsyncIterator[dict]:
        self.opened += 1
        try:
            async for frame in super().subscribe(since):
                yield frame
        finally:
            self.finalised += 1


async def test_repeated_trips_finalise_every_stream_generator():
    # After a trip the stream generator is SUSPENDED at its yield, so only an
    # explicit `aclose()` finalises it. Consumer abandonment (the last connection
    # here) is the most-travelled path and the only one where `aclose()` has real
    # work to do -- every other path has closed the generator by exception
    # already. No-FD-leak across repeated trips is probe-verified against real
    # httpx; this pins the generator half.
    trip = [Advance(200.0), _msg("lost", "discarded")]
    clock = FakeMono()
    stream = CountingStream([trip, trip, trip, [_msg("a1", "delivered")]], clock=clock)
    intake = EventIntake(
        stream,
        mono_clock=clock,
        timeout_ctx=DrivenTimeout(clock),
        liveness_deadline=135.0,
        sleep=_no_sleep,
    )

    agen = intake.events()
    async for _ev in agen:
        break

    assert (stream.opened, stream.finalised) == (4, 3), "a trip leaked a generator"
    await agen.aclose()
    assert stream.finalised == 4, "consumer abandonment leaked the live generator"


async def test_first_proof_of_life_line_fires_once_per_process(caplog):
    script = [_KEEPALIVE, Advance(50.0), _KEEPALIVE, Advance(50.0), _msg("a1", "x")]
    h = _harness([script])
    with caplog.at_level(logging.INFO, logger="henk.events.intake"):
        await _drain(h.intake, limit=1)

    messages = [r.getMessage() for r in caplog.records]
    assert len([m for m in messages if "first proof-of-life" in m]) == 1
    # Three proof-of-life frames, one hour of interval to go: no periodic line yet.
    assert not [m for m in messages if "still delivering" in m]


async def test_periodic_liveness_line_fires_on_its_interval_not_per_frame(caplog):
    # 14 proof-of-life frames across 650s of simulated time against a 600s
    # interval: one line, not one per frame.
    script = [_KEEPALIVE] + [Advance(50.0), _KEEPALIVE] * 12 + [
        Advance(50.0), _msg("a1", "x"),
    ]
    h = _harness([script], liveness_report_interval=600.0)
    with caplog.at_level(logging.INFO, logger="henk.events.intake"):
        await _drain(h.intake, limit=1)

    periodic = [r.getMessage() for r in caplog.records if "still delivering" in r.getMessage()]
    assert len(periodic) == 1, periodic
    # The line carries what a liveness conclusion rests on, plus the frame count
    # that makes the delivery cadence readable from the lines alone.
    assert "12 proof-of-life frames" in periodic[0]
    assert "penalty 0" in periodic[0]
    assert "last reconnect" in periodic[0]


async def test_the_trip_line_carries_a_stable_identifier(caplog):
    # Change D extracts trip counts and inter-trip intervals by matching this
    # token, so it is a contract: the surrounding wording may change, it may not.
    h = _harness([[Advance(200.0), _msg("lost", "x")], [_msg("a1", "after")]])
    with caplog.at_level(logging.INFO, logger="henk.events.intake"):
        await _drain(h.intake, limit=1)

    trips = [r for r in caplog.records if LIVENESS_TRIP_MARKER in r.getMessage()]
    assert len(trips) == 1, [r.getMessage() for r in caplog.records]
    assert trips[0].levelno == logging.WARNING


async def test_a_clean_end_is_not_logged_as_a_failure(caplog):
    # Without a shared backoff helper taking a *reason*, the clean-end path reuses
    # the error path's "event stream failed" line -- flooding a healthy day with
    # failure lines and seeding exactly the misreading D4 exists to prevent.
    slept: list[float] = []
    h = _harness([[_msg("a1", "x")]], sleep=_recording_sleep(slept, stop_after=1))
    with caplog.at_level(logging.INFO, logger="henk.events.intake"):
        await _drain(h.intake)

    messages = [r.getMessage() for r in caplog.records]
    assert not [m for m in messages if "event stream failed" in m]
    assert [m for m in messages if "event stream ended" in m]


class BlockingStream:
    """Yields scripted frames, then blocks forever — a real half-open socket.

    Unlike ``FakeStream``, the silent retrieval actually suspends, so the *real*
    ``asyncio.timeout`` has something to cancel.
    """

    def __init__(self, scripts: list[list]) -> None:
        self._scripts = scripts
        self.since_calls: list[str | None] = []
        self.cancelled = 0
        self._n = 0

    async def subscribe(self, since: str | None) -> AsyncIterator[dict]:
        self.since_calls.append(since)
        script = self._scripts[min(self._n, len(self._scripts) - 1)]
        self._n += 1
        for frame in script:
            yield frame
        try:
            await asyncio.Event().wait()  # never set: the stream has gone silent
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


async def test_a_real_timeout_cancels_the_pending_read_and_intake_recovers():
    # The faked timeout_ctx tests the budget ARITHMETIC and never the
    # cancellation: it raises without cancelling anything. This case uses the real
    # `asyncio.timeout` over a genuinely-suspended read, so it pins what the seam
    # cannot -- that the pending retrieval is cancelled, that CancelledError is
    # handled locally rather than surfacing in the consumer, and that intake
    # resumes from the right cursor afterwards. (httpx's own cancel scopes are
    # still probe-territory; this covers intake's scope only.)
    stream = BlockingStream([[_msg("a1", "before")], [_msg("b2", "after")]])
    intake = EventIntake(
        stream, sleep=_no_sleep, liveness_deadline=0.05  # real seconds
    )
    events = await _drain(intake, limit=2)

    assert [e.title for e in events] == ["before", "after"]
    assert stream.cancelled == 1, "the pending read was not cancelled"
    assert stream.since_calls == [None, "a1"]
