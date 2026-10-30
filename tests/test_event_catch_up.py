"""maintenance-calendar task 6.3/6.4: when intake has caught up (design D8).

Intake reports two things to the coordinator's pump, through callbacks passed to
``EventIntake.events``: every subscribe attempt (which resets the evidence) and every
``keepalive`` frame (and only those: proof of life also counts message frames, which
is exactly what catch-up must not). The probe in ``notes/ntfy-keepalive-probe.md``
showed ntfy sends its cached ``since=`` backlog before the first keepalive, so a
keepalive since the latest subscribe means the backlog has been received.

The coordinator is caught up when, in addition, nothing received is still in the pump
queue, in an open debounce batch, or in a dispatch.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from henk.events.coordinator import EventCoordinator
from henk.events.intake import EventIntake, EventStreamError
from henk.events.pipeline import EventPipeline, PipelineConfig
from tests.conftest import make_clock
from tests.hold_fixtures import (
    Clock,
    RecordingCore,
    ScriptedIntake,
    alert,
    settle,
)


def _msg(mid: str, title: str = "Gatus: svc/api", **extra) -> dict:
    frame = {"id": mid, "event": "message", "topic": "henk-events", "title": title,
             "message": "triggered"}
    frame.update(extra)
    return frame


KEEPALIVE = {"event": "keepalive", "topic": "henk-events"}
OPEN = {"event": "open", "topic": "henk-events"}


class BlockingStream:
    """Per-connection scripts; after a script's frames the connection stays open.

    A script ending in an exception ends that connection with it; the next
    ``subscribe`` takes the next script.
    """

    def __init__(self, scripts: list[list], log: list | None = None) -> None:
        self._scripts = scripts
        self._n = 0
        self.log = log if log is not None else []
        self.hang = asyncio.Event()

    async def subscribe(self, since):
        self.log.append(("stream.subscribe", since))
        script = self._scripts[min(self._n, len(self._scripts) - 1)]
        self._n += 1
        for item in script:
            if isinstance(item, Exception):
                raise item
            yield item
        await self.hang.wait()


async def _no_sleep(_delay: float) -> None:
    return None


async def _consume(agen, out: list, limit: int) -> None:
    async for event in agen:
        out.append(event)
        if len(out) >= limit:
            break


# --- EventIntake: the two callbacks -----------------------------------------------


async def test_on_subscribe_runs_before_every_subscribe_attempt():
    log: list = []
    stream = BlockingStream(
        [[_msg("a1"), EventStreamError("dropped")], [_msg("b2")]], log=log
    )
    intake = EventIntake(stream, sleep=_no_sleep)
    out: list = []
    agen = intake.events(on_subscribe=lambda: log.append("on_subscribe"))
    try:
        await asyncio.wait_for(_consume(agen, out, 2), 1)
    finally:
        await agen.aclose()
    assert log == [
        "on_subscribe", ("stream.subscribe", None),
        "on_subscribe", ("stream.subscribe", "a1"),
    ]


async def test_on_keepalive_fires_for_keepalive_frames_only():
    seen: list = []
    stream = BlockingStream([[OPEN, _msg("a1"), KEEPALIVE, _msg("a2"), KEEPALIVE,
                              _msg("a3")]])
    intake = EventIntake(stream)
    out: list = []
    agen = intake.events(on_keepalive=lambda: seen.append(len(out)))
    try:
        await asyncio.wait_for(_consume(agen, out, 3), 1)
    finally:
        await agen.aclose()
    # Two keepalives, each reported after the consumer had taken every message
    # before it: the open and the three message frames reported nothing.
    assert seen == [1, 2]


async def test_a_failing_callback_never_kills_intake():
    def boom() -> None:
        raise RuntimeError("callback failed")

    stream = BlockingStream([[KEEPALIVE, _msg("a1")]])
    intake = EventIntake(stream)
    out: list = []
    agen = intake.events(on_subscribe=boom, on_keepalive=boom)
    try:
        await asyncio.wait_for(_consume(agen, out, 1), 1)
    finally:
        await agen.aclose()
    assert [e.id for e in out] == ["a1"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (1794078000, 1794078000.0),
        (1794078000.5, 1794078000.5),
        (None, None),
        (True, None),
        ("1794078000", None),
        (0, None),
        (-5, None),
        (math.nan, None),
        (math.inf, None),
    ],
)
async def test_publish_time_comes_from_the_frame_time(value, expected):
    frame = _msg("a1") if value is None else _msg("a1", time=value)
    intake = EventIntake(BlockingStream([[frame]]))
    out: list = []
    agen = intake.events()
    try:
        await asyncio.wait_for(_consume(agen, out, 1), 1)
    finally:
        await agen.aclose()
    assert out[0].published_at == expected


# --- The coordinator's catch-up state ----------------------------------------------


def _coordinator(intake, *, core=None, debounce: float = 0.0, mono=lambda: 0.0,
                 clock=None):
    return EventCoordinator(
        intake,
        EventPipeline(PipelineConfig()),
        core or RecordingCore(),
        debounce_seconds=debounce,
        wall_clock=clock or Clock(1000.0),
        mono_clock=mono,
    )


def test_the_first_subscribe_attempt_is_recorded_once():
    clock = Clock(1000.0)
    coord = _coordinator(ScriptedIntake(), clock=clock)
    assert coord.first_subscribe_attempt_at is None
    coord.note_subscribe()
    clock.now = 2000.0
    coord.note_subscribe()
    assert coord.first_subscribe_attempt_at == 1000.0


def test_caught_up_needs_a_keepalive_since_the_latest_subscribe():
    coord = _coordinator(ScriptedIntake())
    assert coord.caught_up is False
    coord.note_subscribe()
    assert coord.caught_up is False
    coord.note_keepalive()
    assert coord.caught_up is True
    coord.note_subscribe()
    assert coord.caught_up is False


async def test_an_event_waiting_in_the_pump_queue_blocks_catch_up():
    intake = ScriptedIntake()
    coord = _coordinator(intake)
    pump = asyncio.create_task(coord._pump(coord._queue))  # run() not consuming
    try:
        intake.push("subscribe", alert("a1", published=900.0), "keepalive")
        await settle()
        assert coord._queue.qsize() == 1
        assert coord.caught_up is False
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


async def test_an_open_debounce_batch_blocks_catch_up():
    intake = ScriptedIntake()
    core = RecordingCore()
    coord = _coordinator(intake, core=core, debounce=120.0)  # mono clock never moves
    task = asyncio.create_task(coord.run())
    try:
        intake.push("subscribe", alert("a1", published=900.0), "keepalive")
        await settle()
        assert coord._queue.qsize() == 0
        assert coord.dispatch_in_progress is False
        assert coord.caught_up is False  # a1 sits in the open batch
        assert core.turns == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_flushed_batch_is_dispatched_and_catch_up_follows():
    intake = ScriptedIntake()
    core = RecordingCore()
    coord = _coordinator(intake, core=core)
    task = asyncio.create_task(coord.run())
    try:
        intake.push("subscribe", alert("a1", published=900.0), "keepalive")
        await settle()
        assert [t.items[0].event.id for t in core.turns] == ["a1"]
        assert coord.caught_up is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_debounce_split_never_drops_the_completed_batch():
    # The arrival-time debouncer closes a batch when an event arrives past its
    # window; run() must dispatch that completed batch, not discard it.
    intake = ScriptedIntake()
    core = RecordingCore()
    # mono: deadline computed at 0, first wait at 0, then past the deadline.
    coord = _coordinator(intake, core=core, debounce=120.0, mono=make_clock([0, 0, 500]))
    task = asyncio.create_task(coord.run())
    try:
        intake.push(
            alert("a1", "svc/a", published=1.0, arrival=1.0),
            alert("b1", "svc/b", published=300.0, arrival=300.0),
        )
        await settle()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    dispatched = sorted(item.event.id for turn in core.turns for item in turn.items)
    assert dispatched == ["a1", "b1"]


# --- Real intake under the real coordinator --------------------------------------


async def _run_real_intake(scripts: list[list]):
    stream = BlockingStream(scripts)
    intake = EventIntake(stream, sleep=_no_sleep)
    core = RecordingCore()
    coord = _coordinator(intake, core=core)
    task = asyncio.create_task(coord.run())
    await settle()
    return stream, coord, core, task


async def _stop(task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_real_intake_message_frames_alone_are_not_caught_up():
    stream, coord, core, task = await _run_real_intake(
        [[OPEN, _msg("a1", "Gatus: svc/a"), _msg("a2", "Gatus: svc/b")]]
    )
    try:
        assert coord.first_subscribe_attempt_at == 1000.0
        assert coord._queue.qsize() == 0 and coord.dispatch_in_progress is False
        assert coord.caught_up is False
    finally:
        await _stop(task)


async def test_real_intake_keepalive_after_the_backlog_means_caught_up():
    stream, coord, core, task = await _run_real_intake(
        [[OPEN, _msg("a1", "Gatus: svc/a"), _msg("a2", "Gatus: svc/b"), KEEPALIVE]]
    )
    try:
        assert coord.caught_up is True
        dispatched = sorted(i.event.id for t in core.turns for i in t.items)
        assert dispatched == ["a1", "a2"]
    finally:
        await _stop(task)


async def test_real_intake_a_reconnect_voids_the_earlier_keepalive():
    stream, coord, core, task = await _run_real_intake(
        [[OPEN, _msg("a1"), KEEPALIVE, EventStreamError("dropped")], [OPEN]]
    )
    try:
        assert stream.log == [("stream.subscribe", None), ("stream.subscribe", "a1")]
        assert coord.caught_up is False
    finally:
        await _stop(task)


# --- A subscription that ends voids its keepalive at once (orchestrator review) ---

from tests.test_event_intake import Advance, DrivenTimeout, FakeMono, FakeStream  # noqa: E402


def _ending_harness(kind: str, log: list):
    async def sleep(delay: float) -> None:
        log.append("backoff")

    clock = FakeMono()
    second = [_msg("b2")]
    first = {
        "error": [_msg("a1"), EventStreamError("dropped")],
        "eof": [_msg("a1")],
        "liveness": [_msg("a1"), Advance(500.0)],
        "since-rejected": [EventStreamError("bad since", status=400)],
    }[kind]
    stream = FakeStream([first, second], clock=clock)
    kwargs = {"initial_offset": "bad-cursor"} if kind == "since-rejected" else {}
    intake = EventIntake(
        stream, sleep=sleep, mono_clock=clock, timeout_ctx=DrivenTimeout(clock),
        liveness_deadline=135.0, **kwargs,
    )
    return intake


@pytest.mark.parametrize("kind", ["error", "eof", "liveness", "since-rejected"])
async def test_on_unsubscribe_runs_when_a_subscription_ends_before_any_backoff(kind):
    log: list = []
    intake = _ending_harness(kind, log)
    agen = intake.events(
        on_subscribe=lambda: log.append("subscribe"),
        on_unsubscribe=lambda: log.append("unsubscribe"),
    )
    try:
        async for event in agen:
            log.append(event.id)
            if event.id == "b2":
                break
    finally:
        await agen.aclose()  # closing the consumer ends the second subscription too
    first = ["subscribe"] + ([] if kind == "since-rejected" else ["a1"])
    backoff = [] if kind == "since-rejected" else ["backoff"]  # first recovery: no sleep
    assert log == first + ["unsubscribe"] + backoff + [
        "subscribe", "b2", "unsubscribe",
    ]


async def test_a_release_tick_during_the_backoff_after_a_stream_error_finds_nothing(
    tmp_path,
):
    from tests.hold_fixtures import DUE, at, build
    from henk.events.release import HoldReleaser

    rig = build(tmp_path, now=at("20:40"))
    await rig.coord.dispatch_batch([alert("h1", published=at("20:40"))])
    rig.clock.now = DUE + 60

    in_backoff = asyncio.Event()
    leave_backoff = asyncio.Event()

    async def gated_sleep(delay: float) -> None:
        in_backoff.set()
        await leave_backoff.wait()

    stream = BlockingStream(
        [[OPEN, _msg("x1", "Gatus: svc/x"), KEEPALIVE, EventStreamError("dropped")],
         [OPEN, KEEPALIVE]]
    )
    coord = EventCoordinator(
        EventIntake(stream, sleep=gated_sleep),
        rig.pipeline,
        rig.core,
        debounce_seconds=0.0,
        audit=rig.audit,
        holds=rig.ledger,
        windows=rig.windows,
        wall_clock=rig.clock,
        mono_clock=lambda: 0.0,
    )
    releaser = HoldReleaser(rig.ledger, coord, grace_seconds=30 * 60.0,
                            windows=rig.windows, clock=rig.clock)
    task = asyncio.create_task(coord.run())
    try:
        await asyncio.wait_for(in_backoff.wait(), 1)
        await settle()
        assert coord.caught_up is False  # the reconnect's backlog is still unread
        await releaser.tick()
        assert rig.ledger.in_flight == frozenset()
        assert all(t.held_release is None for t in rig.core.turns)

        leave_backoff.set()  # reconnect; the new subscription sends a keepalive
        await settle()
        assert coord.caught_up is True
        await releaser.tick()
        assert [t.held_release.window_id for t in rig.core.turns if t.held_release] == [
            "evt-patch"
        ]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_note_unsubscribe_clears_catch_up():
    coord = _coordinator(ScriptedIntake())
    coord.note_subscribe()
    coord.note_keepalive()
    assert coord.caught_up is True
    coord.note_unsubscribe()
    assert coord.caught_up is False
    coord.note_subscribe()
    coord.note_keepalive()
    assert coord.caught_up is True
