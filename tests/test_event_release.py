"""maintenance-calendar task 6.3: held events are released after the window, the
grace, and intake catch-up.

From the event-intake delta, *Held events are released after the window, the grace,
and intake catch-up* (every scenario), plus the audit-log delta's ``hold-release``
record (exactly one per window, counts of distinct identities). Design D8.

Marking released is split with group 7: the core will call
:meth:`HoldLedger.mark_released` once the release triage's audit record is durable.
Here it is called directly where the core's sink would call it.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from henk.agent.core import AgentCore
from henk.agent.turns import HeldRelease
from henk.audit import AuditLog
from henk.calendar.windows import MaintenanceWindow, MaintenanceWindows
from henk.config import CalendarConfig
from henk.events.checkpoint import OffsetCheckpoint
from henk.events.release import (
    CATCH_UP_FALLBACK_SECONDS,
    RELEASE_TICK_SECONDS,
    HoldReleaser,
)
from henk.store.errors import StoreError
from tests.conftest import EventSessionFactory, FakeChannel
from tests.hold_fixtures import (
    DUE,
    GRACE,
    HOUR,
    MIN,
    PATCH,
    TITLE,
    W_END,
    W_START,
    FakeWindows,
    GatedCore,
    alert,
    at,
    build,
    restart,
    settle,
)

DAY = 24 * HOUR


async def _hold(rig, *events):
    await rig.coord.dispatch_batch(list(events))


def _release_records(rig):
    return [
        {k: r[k] for k in ("window_id", "held", "recovered", "forwarded")}
        for r in rig.records("hold-release")
    ]


# --- Scenario: Recovered alerts send nothing --------------------------------------


async def test_recovered_alerts_send_nothing(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("e1", published=at("20:40")))
    await _hold(rig, alert("e2", state="resolved", published=at("20:55")))
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()

    assert rig.core.turns == []
    assert _release_records(rig) == [
        {"window_id": PATCH.id, "held": 1, "recovered": 1, "forwarded": 0}
    ]
    assert rig.window_row().released_at == DUE
    assert rig.ledger.in_flight == frozenset()


async def test_recovered_alerts_send_nothing_through_the_real_core(tmp_path):
    factory = EventSessionFactory()
    channel = FakeChannel()
    core = AgentCore(factory, channel, audit=AuditLog(tmp_path / "core.jsonl"))
    rig = build(tmp_path, now=at("20:40"), core=core)
    await _hold(rig, alert("e1", published=at("20:40")))
    await _hold(rig, alert("e2", state="resolved", published=at("20:55")))
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()
    while not core._queue.empty():
        await core.process(core._queue.get_nowait())
    assert factory.create_count == 0
    assert channel.sent == []


# --- Scenario: Still-firing alert is triaged after the window --------------------


async def test_still_firing_alert_is_triaged_after_the_window(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", "svc/a", published=at("20:40")))
    await _hold(rig, alert("b1", "svc/b", published=at("20:41")))
    await _hold(rig, alert("b2", "svc/b", state="resolved", published=at("20:50")))
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()

    (turn,) = rig.core.turns
    assert [item.identity.key for item in turn.items] == ["gatus:svc/a"]
    assert turn.held_release == HeldRelease(
        window_id=PATCH.id,
        title=TITLE,
        start=W_START,
        end_with_grace=DUE,
        held=2,
        recovered=1,
        hold_age_seconds=DUE - W_END,
    )
    assert turn.held_release.forwarded == 1
    assert turn.offset is None  # the checkpoint is untouched by a release
    # In flight from submission until marked; not released at submission.
    assert PATCH.id in rig.ledger.in_flight
    assert rig.window_row().released_at is None
    assert rig.records("hold-release") == []

    assert rig.ledger.mark_released(PATCH.id) is True
    assert _release_records(rig) == [
        {"window_id": PATCH.id, "held": 2, "recovered": 1, "forwarded": 1}
    ]
    assert rig.ledger.in_flight == frozenset()


async def test_forwarded_event_is_the_latest_non_resolved_one(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", published=at("20:40"), message="triggered first"))
    await _hold(rig, alert("a2", state="resolved", published=at("20:45")))
    await _hold(rig, alert("a3", published=at("20:50"), message="triggered again"))
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()
    (turn,) = rig.core.turns
    (item,) = turn.items
    assert item.event.id == "a3"
    assert turn.held_release.recovered == 0
    assert turn.held_release.held == 1


async def test_forwarded_event_is_rebuilt_from_the_stored_fields(tmp_path):
    rig = build(tmp_path, now=at("21:10"))
    original = alert("a1", published=at("21:00"), arrival=at("21:10"),
                     message="triggered: 500 on /health")
    await _hold(rig, original)
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()
    (turn,) = rig.core.turns
    rebuilt = turn.items[0].event
    assert rebuilt == original
    assert dict(rebuilt.raw) == dict(original.raw)


# --- Scenario: Release waits for the backlog after a long outage ----------------


async def test_release_waits_for_the_backlog_after_a_long_outage(tmp_path):
    rig = build(tmp_path, now=at("21:00"))
    await _hold(rig, alert("a1", published=at("21:00")))
    # Henk is down 21:50-23:45. The backlog holds A's resolve, published 22:10.
    rig.clock.now = at("23:45")
    rig = restart(rig)
    task = asyncio.create_task(rig.coord.run())
    try:
        await rig.releaser.tick()  # at startup: nothing subscribed yet
        assert rig.core.turns == [] and rig.records("hold-release") == []

        rig.intake.push(
            "subscribe",
            alert("a2", state="resolved", published=at("22:10"), arrival=at("23:45")),
            "keepalive",
        )
        # The backlog is in intake's hands but not dispatched: not caught up.
        await rig.releaser.tick()
        assert rig.records("hold-release") == []

        await settle()
        assert rig.coord.caught_up is True
        await rig.releaser.tick()
        await rig.releaser.tick()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert rig.core.turns == []  # A counted recovered, nothing triaged
    assert _release_records(rig) == [
        {"window_id": PATCH.id, "held": 1, "recovered": 1, "forwarded": 0}
    ]


# --- Scenario: Message frames alone do not mean caught up ------------------------


async def test_message_frames_alone_do_not_mean_caught_up(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = DUE + MIN
    task = asyncio.create_task(rig.coord.run())
    try:
        rig.intake.push(
            "subscribe",
            alert("x1", "svc/x", published=DUE + 10, arrival=DUE + MIN),
            alert("x2", "svc/y", published=DUE + 20, arrival=DUE + MIN),
        )
        await settle()
        assert rig.coord.caught_up is False
        await rig.releaser.tick()
        assert rig.records("hold-release") == []
        assert all(t.held_release is None for t in rig.core.turns)

        rig.intake.push("keepalive")
        await settle()
        assert rig.coord.caught_up is True
        await rig.releaser.tick()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [t.held_release.window_id for t in rig.core.turns if t.held_release] == [
        PATCH.id
    ]


# --- Scenario: Dispatch in progress blocks release --------------------------------


async def test_dispatch_in_progress_blocks_release(tmp_path):
    core = GatedCore()
    rig = build(tmp_path, now=at("20:40"), core=core)
    core.gate.set()
    await _hold(rig, alert("a1", published=at("20:40")))
    core.gate.clear()
    rig.clock.now = DUE + MIN
    task = asyncio.create_task(rig.coord.run())
    try:
        rig.intake.push("subscribe", "keepalive")
        await settle()
        assert rig.coord.caught_up is True

        # A live batch whose dispatch suspends on its submission to the core.
        rig.intake.push(alert("x1", "svc/x", published=DUE + 30, arrival=DUE + MIN))
        await asyncio.wait_for(core.waiting.wait(), 1)
        assert rig.coord.dispatch_in_progress is True
        assert rig.coord.caught_up is False
        await rig.releaser.tick()
        assert rig.records("hold-release") == []
        assert PATCH.id not in rig.ledger.in_flight

        core.gate.set()
        await settle()
        assert rig.coord.dispatch_in_progress is False
        assert rig.coord.caught_up is True
        await rig.releaser.tick()
        assert PATCH.id in rig.ledger.in_flight
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_dispatch_in_progress_is_cleared_when_dispatch_raises(tmp_path, monkeypatch):
    rig = build(tmp_path, now=DUE + MIN)

    async def boom(batch, now=None):
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(rig.coord, "dispatch_batch", boom)
    task = asyncio.create_task(rig.coord.run())
    try:
        rig.intake.push("subscribe", "keepalive", alert("x1", published=DUE))
        await settle()
        assert rig.coord.dispatch_in_progress is False
        assert rig.coord.caught_up is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- Scenario: Unreachable ntfy cannot hold alerts forever -----------------------


@pytest.mark.parametrize("fallback", [CATCH_UP_FALLBACK_SECONDS, 5 * MIN])
async def test_unreachable_ntfy_cannot_hold_alerts_forever(tmp_path, fallback):
    rig = build(tmp_path, now=at("20:40"), fallback_seconds=fallback)
    await _hold(rig, alert("a1", published=at("20:40")))
    # Henk's first subscribe attempt is long after the window's end plus grace.
    first = DUE + 2 * HOUR
    rig.clock.now = first
    rig.coord.note_subscribe()  # no keepalive, ever

    rig.clock.now = first + fallback - 1
    await rig.releaser.tick()
    assert rig.core.turns == []  # counted from the first subscribe, not from 22:30

    rig.clock.now = first + fallback
    await rig.releaser.tick()
    assert len(rig.core.turns) == 1


@pytest.mark.parametrize("fallback", [CATCH_UP_FALLBACK_SECONDS, 5 * MIN])
async def test_the_fallback_counts_from_the_end_plus_grace_when_that_is_later(
    tmp_path, fallback
):
    rig = build(tmp_path, now=at("20:40"), fallback_seconds=fallback)
    rig.coord.note_subscribe()  # Henk has been subscribing since 20:40
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = DUE + fallback - 1
    await rig.releaser.tick()
    assert rig.core.turns == []
    rig.clock.now = DUE + fallback
    await rig.releaser.tick()
    assert len(rig.core.turns) == 1


async def test_the_default_fallback_is_fifteen_minutes(tmp_path):
    assert CATCH_UP_FALLBACK_SECONDS == 15 * MIN
    rig = build(tmp_path, now=at("20:40"))  # releaser built with its defaults
    rig.coord.note_subscribe()
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = DUE + 15 * MIN - 1
    await rig.releaser.tick()
    assert rig.core.turns == []
    rig.clock.now = DUE + 15 * MIN
    await rig.releaser.tick()
    assert len(rig.core.turns) == 1


async def test_without_any_subscribe_attempt_the_fallback_never_fires(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = DUE + 3 * DAY
    await rig.releaser.tick()
    assert rig.core.turns == []


async def test_a_keepalive_before_the_latest_subscribe_does_not_count(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = DUE + MIN
    rig.coord.note_subscribe()
    rig.coord.note_keepalive()
    rig.coord.note_subscribe()  # reconnect: the earlier keepalive is void
    assert rig.coord.caught_up is False
    await rig.releaser.tick()
    assert rig.core.turns == []


# --- Not before the window's end plus grace ---------------------------------------


@pytest.mark.parametrize("grace", [GRACE, 10 * MIN])
@pytest.mark.parametrize("calendar", ["unchanged", "event deleted", "disabled"])
async def test_nothing_is_released_before_the_stored_end_plus_grace(
    tmp_path, grace, calendar
):
    rig = build(tmp_path, now=at("20:40"), grace=grace)
    await _hold(rig, alert("a1", published=at("20:40")))
    if calendar == "event deleted":
        rig.windows.windows = []  # the stored end still holds until end + grace
    elif calendar == "disabled":
        rig = restart(rig, windows=None)
    rig.caught_up()
    rig.clock.now = W_END + grace - 1
    await rig.releaser.tick()
    assert rig.core.turns == []
    rig.clock.now = W_END + grace
    await rig.releaser.tick()
    (turn,) = rig.core.turns
    assert turn.held_release.end_with_grace == W_END + grace


# --- Scenario: Crash before the triage is durable releases again -----------------


def _core(tmp_path, checkpoint):
    factory = EventSessionFactory()
    core = AgentCore(
        factory,
        FakeChannel(),
        audit=AuditLog(tmp_path / "core.jsonl"),
        checkpoint=checkpoint,
    )
    return core, factory


async def _drain(core):
    while not core._queue.empty():
        await core.process(core._queue.get_nowait())


async def test_crash_before_the_triage_is_durable_releases_again(tmp_path):
    checkpoint_path = tmp_path / "offset"
    checkpoint = OffsetCheckpoint(checkpoint_path)
    checkpoint.write("e0")
    core, _ = _core(tmp_path, checkpoint)
    rig = build(tmp_path, now=at("20:40"), core=core)
    await _hold(rig, alert("a1", published=at("20:40")))
    await _drain(core)  # the held-only batch's marker
    before = checkpoint_path.read_bytes()
    assert before == b"a1"

    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()
    assert core._queue.qsize() == 1  # submitted, and the process is killed here
    assert checkpoint_path.read_bytes() == before

    core2, factory2 = _core(tmp_path, OffsetCheckpoint(checkpoint_path))
    rig2 = restart(rig, core=core2)
    assert rig2.window_row().released_at is None
    rig2.caught_up()
    await rig2.releaser.tick()
    (turn,) = list(core2._queue._queue)
    assert turn.held_release.window_id == PATCH.id
    await _drain(core2)
    assert factory2.create_count == 1  # released again, triaged
    assert checkpoint_path.read_bytes() == before  # no offset, so no advance
    # Group 7's sink marks it after the triage record is durable.
    assert rig2.ledger.mark_released(PATCH.id) is True
    assert len(rig2.records("hold-release")) == 1


# --- Scenario: Release survives a restart -----------------------------------------


async def test_release_survives_a_restart(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = DUE + 2 * HOUR  # Henk was down when 22:30 passed
    rig = restart(rig)
    await rig.releaser.tick()
    assert rig.core.turns == []
    rig.caught_up()
    await rig.releaser.tick()
    (turn,) = rig.core.turns
    assert turn.held_release.window_id == PATCH.id
    await rig.releaser.tick()
    assert len(rig.core.turns) == 1  # in flight: never submitted twice


# --- Scenarios: Extended window defers release; moved window is not an extension


async def test_extended_window_defers_release(tmp_path):
    windows = FakeWindows(PATCH)
    rig = build(tmp_path, now=at("20:40"), windows=windows)
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.caught_up()
    # The owner extends the end from 22:00 to 23:00; the refresh has seen it.
    windows.windows = [MaintenanceWindow(PATCH.id, TITLE, W_START, W_END + HOUR)]
    rig.clock.now = at("22:30")
    await rig.releaser.tick()
    assert rig.core.turns == []
    assert rig.window_row().end == W_END + HOUR

    rig.clock.now = at("23:29")
    await rig.releaser.tick()
    assert rig.core.turns == []
    rig.clock.now = at("23:30")
    await rig.releaser.tick()
    (turn,) = rig.core.turns
    assert turn.held_release.end_with_grace == at("23:30")


async def test_extension_is_seen_through_the_real_window_provider(tmp_path):
    from henk.calendar.client import CalendarEvent

    class Source:
        events = [CalendarEvent(id=PATCH.id, title=TITLE, start=W_START, end=W_END + HOUR)]

        async def list_window(self, time_min, time_max):
            return list(self.events)

    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", published=at("20:40")))
    provider = MaintenanceWindows(
        Source(),
        CalendarConfig(enabled=True, calendar_id="cal-placeholder"),
        audit_path=tmp_path / "henk-audit.jsonl",
        events_enabled=True,
        clock=rig.clock,
    )
    rig.clock.now = at("22:25")
    assert await provider.refresh() is True
    releaser = HoldReleaser(
        rig.ledger, rig.coord, grace_seconds=GRACE, windows=provider, clock=rig.clock
    )
    rig.caught_up()
    rig.clock.now = at("22:30")
    await releaser.tick()
    assert rig.core.turns == []
    assert rig.window_row().end == W_END + HOUR


async def test_moved_window_is_not_an_extension(tmp_path):
    windows = FakeWindows(PATCH)
    rig = build(tmp_path, now=at("20:40"), windows=windows)
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.caught_up()
    week = 7 * DAY
    windows.windows = [MaintenanceWindow(PATCH.id, TITLE, W_START + week, W_END + week)]
    rig.clock.now = at("22:30")
    await rig.releaser.tick()
    (turn,) = rig.core.turns
    assert turn.held_release.end_with_grace == DUE
    assert rig.window_row().end == W_END


async def test_a_window_now_starting_after_its_earliest_hold_is_not_an_extension(tmp_path):
    windows = FakeWindows(PATCH)
    rig = build(tmp_path, now=at("20:40"), windows=windows)
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.caught_up()
    # Same id, active now, but it starts at 22:20, after the 20:40 hold.
    windows.windows = [MaintenanceWindow(PATCH.id, TITLE, at("22:20"), at("23:20"))]
    rig.clock.now = at("22:30")
    await rig.releaser.tick()
    assert len(rig.core.turns) == 1
    assert rig.window_row().end == W_END


async def test_a_window_starting_exactly_at_the_earliest_hold_is_an_extension(tmp_path):
    windows = FakeWindows(PATCH)
    rig = build(tmp_path, now=at("20:40"), windows=windows)
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.caught_up()
    windows.windows = [MaintenanceWindow(PATCH.id, TITLE, at("20:40"), at("23:00"))]
    rig.clock.now = at("22:30")
    await rig.releaser.tick()
    assert rig.core.turns == []
    assert rig.window_row().end == at("23:00")


# --- Scenario: Release obeys cooldown and the daily cap --------------------------


async def test_release_obeys_cooldown_and_the_daily_cap(tmp_path):
    rig = build(tmp_path, now=at("19:50"), cap=1)
    # A fired and was triaged live before the window: cooldown armed, cap spent.
    await rig.coord.dispatch_batch([alert("a0", "svc/a", published=at("19:50"))])
    assert rig.core.turns[0].announceable is True
    rig.core.turns.clear()

    rig.clock.now = at("20:40")
    await _hold(rig, alert("a1", "svc/a", published=at("20:40")))
    await _hold(rig, alert("b1", "svc/b", published=at("20:41")))
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()

    (suppression,) = rig.records("suppression")
    assert suppression["identity_key"] == "gatus:svc/a"
    assert suppression["reason"] == "cooldown"
    assert suppression["event_id"] == "a1"
    (turn,) = rig.core.turns
    assert [item.identity.key for item in turn.items] == ["gatus:svc/b"]
    assert turn.announceable is False
    assert turn.held_release.held == 2


async def test_a_release_whose_every_identity_cools_down_marks_itself(tmp_path):
    rig = build(tmp_path, now=at("19:50"))
    await rig.coord.dispatch_batch([alert("a0", "svc/a", published=at("19:50"))])
    rig.core.turns.clear()
    rig.clock.now = at("20:40")
    await _hold(rig, alert("a1", "svc/a", published=at("20:40")))
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()
    assert rig.core.turns == []
    assert len(rig.records("suppression")) == 1
    assert _release_records(rig) == [
        {"window_id": PATCH.id, "held": 1, "recovered": 0, "forwarded": 1}
    ]
    assert rig.window_row().released_at == DUE


# --- Scenario: Release runs with the calendar disabled ----------------------------


async def test_release_runs_with_the_calendar_disabled(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = DUE + MIN
    rig = restart(rig, windows=None)  # calendar.enabled false, restarted
    rig.caught_up()
    await rig.releaser.tick()
    (turn,) = rig.core.turns
    assert turn.held_release.window_id == PATCH.id


# --- The release task: at startup, then every 60 seconds -------------------------


class _Stop(Exception):
    pass


@pytest.mark.parametrize("interval", [RELEASE_TICK_SECONDS, 17.0])
async def test_the_release_task_ticks_at_startup_then_every_interval(tmp_path, interval):
    assert RELEASE_TICK_SECONDS == 60.0
    slept: list[float] = []
    ticks: list[str] = []

    async def spy_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            raise _Stop

    kwargs = {} if interval == RELEASE_TICK_SECONDS else {"tick_seconds": interval}
    rig = build(tmp_path, now=at("20:40"), sleep=spy_sleep, **kwargs)
    original = rig.releaser.tick

    async def counting_tick(now=None):
        ticks.append("tick")
        await original(now)

    rig.releaser.tick = counting_tick
    with pytest.raises(_Stop):
        await rig.releaser.run()
    assert slept == [interval] * 3
    assert ticks == ["tick"] * 3  # the first before any sleep


async def test_a_failing_tick_does_not_stop_the_release_task(tmp_path):
    slept: list[float] = []

    async def spy_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            raise _Stop

    rig = build(tmp_path, now=at("20:40"), sleep=spy_sleep)

    async def broken(now=None):
        raise RuntimeError("tick failed")

    rig.releaser.tick = broken
    with pytest.raises(_Stop):
        await rig.releaser.run()
    assert slept == [RELEASE_TICK_SECONDS, RELEASE_TICK_SECONDS]


async def test_one_failing_window_does_not_block_another(tmp_path, monkeypatch):
    other = MaintenanceWindow(id="w-other", title="Homelab: b", start=at("20:00"),
                              end=at("21:50"))
    rig = build(tmp_path, now=at("20:40"), windows=FakeWindows(PATCH, other))
    await _hold(rig, alert("a1", published=at("20:40")))
    # Force a second window row directly through the ledger's store.
    rig.holds.hold(
        window_id="w-other", window_title="Homelab: b", window_start=at("20:00"),
        window_end=at("21:50"), event_id="b1", identity_key="gatus:svc/b",
        state="resolved", title="Gatus: svc/b", message="resolved",
        raw_json=json.dumps({"id": "b1"}), published_at=at("20:41"),
        arrival_time=at("20:41"), held_at=at("20:41"),
    )
    original = rig.holds.held_events

    def flaky(window_id):
        if window_id == PATCH.id:
            raise StoreError("read failed")
        return original(window_id)

    monkeypatch.setattr(rig.holds, "held_events", flaky)
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()
    assert [r["window_id"] for r in rig.records("hold-release")] == ["w-other"]


# --- Marking released ----------------------------------------------------------------


async def test_a_second_mark_writes_no_second_hold_release(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()
    assert rig.ledger.mark_released(PATCH.id) is True
    assert rig.ledger.mark_released(PATCH.id) is False
    assert len(rig.records("hold-release")) == 1


async def test_a_failed_mark_keeps_the_window_in_flight(tmp_path, monkeypatch):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()

    def broken(window_id, at):
        raise StoreError("disk full")

    monkeypatch.setattr(rig.holds, "mark_released", broken)
    assert rig.ledger.mark_released(PATCH.id) is False
    assert PATCH.id in rig.ledger.in_flight  # not re-released until a restart
    await rig.releaser.tick()
    assert len(rig.core.turns) == 1
    assert rig.records("hold-release") == []


async def test_a_failed_hold_release_record_still_marks_the_window(tmp_path, caplog):
    class ReleaseRecordFails(AuditLog):
        def write(self, record):
            if record.get("record_type") == "hold-release":
                return False
            return super().write(record)

    rig = build(tmp_path, now=at("20:40"),
                audit=ReleaseRecordFails(tmp_path / "henk-audit.jsonl"))
    await _hold(rig, alert("a1", published=at("20:40")))
    await _hold(rig, alert("a2", state="resolved", published=at("20:50")))
    rig.clock.now = DUE
    rig.caught_up()
    import logging

    with caplog.at_level(logging.ERROR):
        await rig.releaser.tick()
    assert rig.window_row().released_at == DUE
    assert any("hold-release" in r.getMessage() for r in caplog.records)


async def test_hold_age_is_measured_from_the_stored_end(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = W_END + 30 * HOUR
    rig.caught_up()
    await rig.releaser.tick()
    (turn,) = rig.core.turns
    assert turn.held_release.hold_age_seconds == 30 * HOUR


async def test_a_failed_submission_keeps_the_window_for_the_next_restart(tmp_path):
    # The failed attempt already armed the identity's cooldown in this process, so
    # a retry here would suppress it and mark the window released with nothing
    # triaged. The window stays in flight instead, and a restart releases it.
    class FailingCore:
        def __init__(self) -> None:
            self.turns: list = []
            self.markers: list = []

        async def submit_event(self, turn) -> None:
            raise RuntimeError("queue gone")

        async def submit_marker(self, marker) -> None:
            self.markers.append(marker)

    rig = build(tmp_path, now=at("20:40"), core=FailingCore())
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()  # the failure is logged, not raised
    assert rig.ledger.in_flight == frozenset({PATCH.id})
    await rig.releaser.tick()
    assert rig.window_row().released_at is None
    assert rig.records("hold-release") == []

    rig = restart(rig)
    rig.caught_up()
    await rig.releaser.tick()
    (turn,) = rig.core.turns
    assert [item.event.id for item in turn.items] == ["a1"]


async def test_forwarded_events_reach_the_pipeline_in_publish_order(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("b1", "svc/b", published=at("20:40")))
    await _hold(rig, alert("a1", "svc/a", published=at("20:45")))
    await _hold(rig, alert("b2", "svc/b", published=at("20:50")))
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()
    (turn,) = rig.core.turns
    # Ordered by each identity's forwarded (latest) event: a1 at 20:45, b2 at 20:50.
    assert [item.event.id for item in turn.items] == ["a1", "b2"]


def test_the_releaser_refuses_a_ledger_the_coordinator_does_not_use(tmp_path):
    # One in-flight set: with two ledgers, a submitted window would be released
    # again on every tick.
    from henk.audit.logger import HoldReceipts
    from henk.events.holds import HoldLedger

    rig = build(tmp_path)
    other = HoldLedger(rig.holds, HoldReceipts(None))
    with pytest.raises(ValueError, match="share one HoldLedger"):
        HoldReleaser(other, rig.coord, grace_seconds=GRACE)


async def test_an_ended_subscription_blocks_release_until_the_next_keepalive(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await _hold(rig, alert("a1", published=at("20:40")))
    rig.clock.now = DUE + MIN
    task = asyncio.create_task(rig.coord.run())
    try:
        rig.intake.push("subscribe", "keepalive", "unsubscribe")
        await settle()
        assert rig.coord.caught_up is False
        await rig.releaser.tick()
        assert rig.core.turns == []

        rig.intake.push("subscribe", "keepalive")
        await settle()
        await rig.releaser.tick()
        (turn,) = rig.core.turns
        assert turn.held_release.window_id == PATCH.id
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
