"""maintenance-calendar task 6.1: events published inside an open window are held.

From the event-intake delta, *Events published inside an open maintenance window are
held* (every scenario), the incident-triage delta's *No triage while held*, and the
audit-log delta's ``hold`` record (no payload; no second record on a duplicate).
Design D7: classification runs in ``dispatch_batch`` ahead of ``pipeline.evaluate``,
in the order duplicate, publish time, open window, hold, and never returns early.
"""

from __future__ import annotations

import json
import logging

from henk.agent.core import AgentCore
from henk.agent.turns import CheckpointMarker
from henk.audit import AuditLog
from henk.calendar.windows import MaintenanceWindow
from henk.events.checkpoint import OffsetCheckpoint
from henk.events.holds import publish_time
from henk.store.errors import StoreError
from tests.conftest import EventSessionFactory, FakeChannel
from tests.hold_fixtures import (
    DUE,
    HOUR,
    PATCH,
    W_END,
    W_START,
    FakeWindows,
    alert,
    at,
    build,
)


def _held_ids(rig, window_id: str = PATCH.id) -> list[str]:
    return [r.event_id for r in rig.holds.held_events(window_id)]


# --- Scenario: Alert during a window is held silently ---------------------------


async def test_alert_during_a_window_is_held_silently(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    event = alert("e1", published=at("20:40"))
    await rig.coord.dispatch_batch([event])

    assert rig.core.turns == []  # no triage session, so no Signal message
    assert _held_ids(rig) == ["e1"]
    (record,) = rig.records("hold")
    assert record["identity_key"] == "gatus:svc/api"
    assert record["event_id"] == "e1"
    assert record["window_id"] == PATCH.id
    assert record["window_end"] == W_END
    assert record["published_at"] == at("20:40")
    assert record["state"] == "firing"
    assert rig.records("suppression") == []

    # No cooldown armed: the same identity firing live after the window is triaged.
    rig.clock.now = DUE + HOUR
    await rig.coord.dispatch_batch([alert("e2", published=DUE + HOUR)])
    assert len(rig.core.turns) == 1
    assert rig.core.turns[0].items[0].event.id == "e2"


async def test_no_triage_session_exists_while_held(tmp_path):
    # incident-triage *No triage while held*, through the real core: the held-only
    # batch reaches the core only as a checkpoint marker.
    factory = EventSessionFactory()
    channel = FakeChannel()
    core = AgentCore(factory, channel, audit=AuditLog(tmp_path / "core.jsonl"))
    rig = build(tmp_path, now=at("20:40"), core=core)
    await rig.coord.dispatch_batch([alert("e1", published=at("20:40"))])
    while not core._queue.empty():
        await core.process(core._queue.get_nowait())
    assert factory.create_count == 0
    assert channel.sent == []


async def test_the_hold_record_carries_no_payload_and_no_window_title(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    secret = "disk /dev/sda1 on rp5 at 97% UNIQUE-PAYLOAD"
    await rig.coord.dispatch_batch(
        [alert("e1", published=at("20:40"), message=f"triggered {secret}")]
    )
    (record,) = rig.records("hold")
    text = json.dumps(record)
    assert "UNIQUE-PAYLOAD" not in text
    assert PATCH.title not in text
    assert "Gatus: svc/api" not in text
    # ...while the held row keeps the payload for the release.
    (row,) = rig.holds.held_events(PATCH.id)
    assert "UNIQUE-PAYLOAD" in row.message
    assert "UNIQUE-PAYLOAD" in row.raw_json


# --- Scenario: Publish time decides after a reboot --------------------------------


async def test_publish_time_decides_after_a_reboot(tmp_path):
    # Published 21:50 inside 20:00-22:00, first delivered at 22:45 from the backlog.
    rig = build(tmp_path, now=at("22:45"))
    await rig.coord.dispatch_batch(
        [alert("e1", published=at("21:50"), arrival=at("22:45"))]
    )
    assert rig.core.turns == []
    assert _held_ids(rig) == ["e1"]


async def test_an_event_published_before_the_window_is_not_held_however_late_it_arrives(
    tmp_path,
):
    rig = build(tmp_path, now=at("20:05"))
    await rig.coord.dispatch_batch(
        [alert("e1", published=at("19:55"), arrival=at("20:05"))]
    )
    assert len(rig.core.turns) == 1
    assert _held_ids(rig) == []


async def test_publish_time_falls_back_to_arrival_time():
    event = alert("e1", arrival=at("20:40"))
    assert event.published_at is None
    assert publish_time(event) == at("20:40")
    assert publish_time(alert("e2", published=at("19:00"), arrival=at("20:40"))) == at(
        "19:00"
    )


async def test_an_event_without_a_publish_time_is_keyed_on_arrival(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await rig.coord.dispatch_batch([alert("e1", arrival=at("20:40"))])
    assert _held_ids(rig) == ["e1"]


# --- Scenario: Mixed batch splits -------------------------------------------------


async def test_mixed_batch_splits(tmp_path):
    rig = build(tmp_path, now=at("20:41"))
    inside = alert("e1", "svc/api", published=at("20:40"))
    before = alert("e2", "svc/db", published=at("19:50"), arrival=at("20:41"))
    await rig.coord.dispatch_batch([inside, before])

    assert _held_ids(rig) == ["e1"]
    (turn,) = rig.core.turns
    assert [item.event.id for item in turn.items] == ["e2"]
    assert turn.announceable is True
    # The survivors' turn carries the whole batch's offset: the held event is
    # already durable, so advancing past it is safe (D7).
    assert turn.offset == "e2"
    assert rig.core.markers == []


# --- Scenario: Held-only batch advances the checkpoint ---------------------------


async def test_held_only_batch_advances_the_checkpoint(tmp_path):
    rig = build(tmp_path, now=at("20:42"))
    await rig.coord.dispatch_batch(
        [
            alert("e1", "svc/api", published=at("20:40")),
            alert("e2", "svc/db", published=at("20:41")),
        ]
    )
    assert rig.core.turns == []
    assert rig.core.markers == [CheckpointMarker(offset="e2")]


async def test_held_only_batch_moves_the_real_checkpoint_past_its_last_event(tmp_path):
    checkpoint = OffsetCheckpoint(tmp_path / "offset")
    checkpoint.write("e0")
    core = AgentCore(
        EventSessionFactory(),
        FakeChannel(),
        audit=AuditLog(tmp_path / "core.jsonl"),
        checkpoint=checkpoint,
    )
    rig = build(tmp_path, now=at("20:42"), core=core)
    await rig.coord.dispatch_batch(
        [alert("e1", published=at("20:40")), alert("e2", published=at("20:41"))]
    )
    while not core._queue.empty():
        await core.process(core._queue.get_nowait())
    assert checkpoint.read() == "e2"


# --- Scenario: A hold that cannot be stored fails open ---------------------------


async def test_a_hold_that_cannot_be_stored_fails_open(tmp_path, monkeypatch, caplog):
    rig = build(tmp_path, now=at("20:40"))

    def broken(**_kwargs):
        raise StoreError("disk full")

    monkeypatch.setattr(rig.holds, "hold", broken)
    with caplog.at_level(logging.ERROR):
        await rig.coord.dispatch_batch([alert("e1", published=at("20:40"))])

    (turn,) = rig.core.turns  # processed as if no window existed
    assert [item.event.id for item in turn.items] == ["e1"]
    assert turn.offset == "e1"
    assert rig.records("hold") == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_an_unusable_store_fails_open_for_every_event(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    rig.store._path = blocker / "henk.db"  # every connect now fails
    await rig.coord.dispatch_batch(
        [alert("e1", "svc/api", published=at("20:40")),
         alert("e2", "svc/db", published=at("20:40"))]
    )
    (turn,) = rig.core.turns
    assert sorted(item.event.id for item in turn.items) == ["e1", "e2"]


async def test_a_failed_hold_record_keeps_the_hold(tmp_path, caplog):
    class HoldRecordFails(AuditLog):
        def write(self, record):
            if record.get("record_type") == "hold":
                return False
            return super().write(record)

    rig = build(tmp_path, now=at("20:40"), audit=HoldRecordFails(tmp_path / "a.jsonl"))
    with caplog.at_level(logging.ERROR):
        await rig.coord.dispatch_batch([alert("e1", published=at("20:40"))])
    assert rig.core.turns == []  # the hold stands: the row is the durable fact
    assert _held_ids(rig) == ["e1"]
    # ...and the marker still advances: only the held ROW gates it (E6).
    assert rig.core.markers == [CheckpointMarker(offset="e1")]
    assert any(
        r.levelno == logging.ERROR and "hold" in r.getMessage() for r in caplog.records
    )


# --- Scenario: Duplicate replay is dropped and still advances -------------------


async def _replay_of_a_held_event(tmp_path, *, released: bool):
    rig = build(tmp_path, now=at("20:40"))
    await rig.coord.dispatch_batch([alert("e1", published=at("20:40"))])
    if released:
        assert rig.ledger.mark_released(PATCH.id) is True
    rig.core.markers.clear()
    rig.clock.now = at("23:50")
    await rig.coord.dispatch_batch([alert("e1", published=at("20:40"), arrival=at("23:50"))])
    return rig


async def test_duplicate_replay_is_dropped_and_still_advances(tmp_path):
    rig = await _replay_of_a_held_event(tmp_path, released=False)
    assert _held_ids(rig) == ["e1"]
    assert len(rig.records("hold")) == 1
    assert rig.core.turns == []
    assert rig.core.markers == [CheckpointMarker(offset="e1")]


async def test_duplicate_replay_after_release_is_dropped_and_still_advances(tmp_path):
    rig = await _replay_of_a_held_event(tmp_path, released=True)
    assert _held_ids(rig) == ["e1"]
    assert len(rig.records("hold")) == 1
    assert rig.core.turns == []  # dropped, not triaged live as a late event
    assert rig.core.markers == [CheckpointMarker(offset="e1")]
    assert len(rig.records("hold-release")) == 1


async def test_a_duplicate_inside_one_batch_is_held_once(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    event = alert("e1", published=at("20:40"))
    await rig.coord.dispatch_batch([event, event])
    assert _held_ids(rig) == ["e1"]
    assert len(rig.records("hold")) == 1
    assert rig.core.turns == []


async def test_duplicates_are_dropped_with_the_calendar_disabled(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await rig.coord.dispatch_batch([alert("e1", published=at("20:40"))])
    # Restart with the calendar off: no window source, holds nothing, but a
    # replayed held event is still a duplicate.
    from tests.hold_fixtures import restart

    rig = restart(rig, windows=None)
    await rig.coord.dispatch_batch(
        [alert("e1", published=at("20:40")), alert("e2", "svc/db", published=at("20:45"))]
    )
    (turn,) = rig.core.turns
    assert [item.event.id for item in turn.items] == ["e2"]
    assert _held_ids(rig) == ["e1"]


async def test_no_window_source_holds_nothing(tmp_path):
    rig = build(tmp_path, now=at("20:40"), windows=None)
    await rig.coord.dispatch_batch([alert("e1", published=at("20:40"))])
    assert len(rig.core.turns) == 1
    assert rig.holds.unreleased_windows() == []


# --- Scenarios: late event for a released window; event during an in-flight release


async def test_late_event_for_a_released_window_is_triaged_live(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await rig.coord.dispatch_batch([alert("e1", published=at("20:40"))])
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()
    assert len(rig.core.turns) == 1  # the release turn
    assert rig.ledger.mark_released(PATCH.id) is True
    rig.core.turns.clear()

    # Published inside W (21:59), delivered only now.
    rig.clock.now = DUE + 10 * 60
    late = alert("e2", "svc/db", published=at("21:59"), arrival=rig.clock.now)
    await rig.coord.dispatch_batch([late])
    (turn,) = rig.core.turns
    assert [item.event.id for item in turn.items] == ["e2"]
    assert turn.held_release is None  # a live turn, not a second release
    assert _held_ids(rig) == ["e1"]

    await rig.releaser.tick()
    assert len(rig.core.turns) == 1  # W is not released again
    assert len(rig.records("hold-release")) == 1


async def test_event_during_an_in_flight_release_is_triaged_live(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await rig.coord.dispatch_batch([alert("e1", published=at("20:40"))])
    rig.clock.now = DUE
    rig.caught_up()
    await rig.releaser.tick()
    (release_turn,) = rig.core.turns
    assert release_turn.held_release.window_id == PATCH.id
    assert PATCH.id in rig.ledger.in_flight

    # Dispatched while W's release is submitted but not yet durable.
    rig.clock.now = DUE + 30
    late = alert("e2", "svc/db", published=at("21:58"), arrival=rig.clock.now)
    await rig.coord.dispatch_batch([late])
    assert len(rig.core.turns) == 2
    live = rig.core.turns[1]
    assert [item.event.id for item in live.items] == ["e2"]
    assert live.offset == "e2"
    assert live.held_release is None
    assert _held_ids(rig) == ["e1"]

    assert rig.ledger.mark_released(PATCH.id) is True
    assert rig.ledger.mark_released(PATCH.id) is False
    await rig.releaser.tick()
    assert len(rig.core.turns) == 2
    assert len(rig.records("hold-release")) == 1


# --- Holding charges no cap and arms no cooldown ---------------------------------


async def test_held_events_charge_no_cap(tmp_path):
    rig = build(tmp_path, now=at("20:40"), cap=1)
    await rig.coord.dispatch_batch([alert("e1", "svc/api", published=at("20:40"))])
    await rig.coord.dispatch_batch([alert("e2", "svc/db", published=at("20:50"))])
    rig.clock.now = DUE + HOUR
    await rig.coord.dispatch_batch([alert("e3", "svc/web", published=DUE + HOUR)])
    (turn,) = rig.core.turns
    assert turn.announceable is True  # the cap of 1 was not spent by the holds


# --- Which window ------------------------------------------------------------------


async def test_held_against_the_open_window_with_the_latest_end(tmp_path):
    early = MaintenanceWindow(id="w-early", title="Homelab: a", start=W_START, end=W_END)
    late = MaintenanceWindow(
        id="w-late", title="Homelab: b", start=W_START + HOUR, end=W_END + HOUR
    )
    rig = build(tmp_path, now=at("21:30"), windows=FakeWindows(early, late))
    await rig.coord.dispatch_batch([alert("e1", published=at("21:30"))])
    assert _held_ids(rig, "w-late") == ["e1"]
    assert _held_ids(rig, "w-early") == []


async def test_a_released_window_does_not_take_a_hold_from_an_overlapping_open_one(
    tmp_path,
):
    early = MaintenanceWindow(id="w-early", title="Homelab: a", start=W_START, end=W_END)
    late = MaintenanceWindow(
        id="w-late", title="Homelab: b", start=W_START + HOUR, end=W_END + HOUR
    )
    rig = build(tmp_path, now=at("21:30"), windows=FakeWindows(early, late))
    await rig.coord.dispatch_batch([alert("e1", published=at("21:30"))])
    assert rig.ledger.mark_released("w-late") is True
    await rig.coord.dispatch_batch([alert("e2", "svc/db", published=at("21:40"))])
    # w-late is closed; w-early is still open and active at 21:40.
    assert _held_ids(rig, "w-early") == ["e2"]


async def test_an_event_without_an_id_is_never_held(tmp_path):
    rig = build(tmp_path, now=at("20:40"))
    await rig.coord.dispatch_batch([alert("", published=at("20:40"))])
    assert len(rig.core.turns) == 1
    assert rig.holds.unreleased_windows() == []


async def test_the_frozen_end_is_recorded_on_every_hold_of_the_window(tmp_path):
    windows = FakeWindows(PATCH)
    rig = build(tmp_path, now=at("20:40"), windows=windows)
    await rig.coord.dispatch_batch([alert("e1", published=at("20:40"))])
    # The calendar now says the window runs an hour longer; the row's end stays.
    windows.windows = [MaintenanceWindow(PATCH.id, PATCH.title, W_START, W_END + HOUR)]
    await rig.coord.dispatch_batch([alert("e2", "svc/db", published=at("21:00"))])
    assert [r["window_end"] for r in rig.records("hold")] == [W_END, W_END]
    assert rig.window_row().end == W_END
