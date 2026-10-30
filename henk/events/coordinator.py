"""Glue: intake → debounce → hold → pipeline → core, plus suppression audit records.

:meth:`EventCoordinator.dispatch_batch` is the pure decision step (hold what a
maintenance window covers, evaluate the rest, write suppression records, submit any
resulting event turn) and is unit-tested. :meth:`run` wraps it in the async
debounce timer that flushes a quiet batch.

Maintenance windows (maintenance-calendar D7, D8):

- ``dispatch_batch`` classifies each batch before ``pipeline.evaluate``: duplicate
  of a held event, then publish time, then open window, then hold. It never returns
  early, so a batch whose events were all held or dropped still advances the
  checkpoint through the existing marker branch.
- :attr:`caught_up` says whether everything intake has received has been
  dispatched: a keepalive since the latest subscribe, an empty pump queue, no open
  debounce batch, and no dispatch in progress. The release
  (:mod:`henk.events.release`) waits for it.
- :meth:`submit_release` evaluates a release's forwarded events like a live batch,
  without an offset.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Protocol, Sequence

from dataclasses import replace

from henk.audit import suppression_record
from henk.agent.turns import CheckpointMarker, HeldRelease
from henk.events.holds import HoldLedger, HoldResult, publish_time
from henk.events.intake import EventIntake
from henk.events.pipeline import Debouncer, EventPipeline, SuppressionRecord
from henk.events.types import Event

logger = logging.getLogger("henk.events.coordinator")


class _EventSink:
    async def submit_event(self, turn) -> None: ...

    async def submit_marker(self, marker) -> None: ...


class _WindowSource(Protocol):
    """The part of :class:`henk.calendar.windows.MaintenanceWindows` used here."""

    def active(self, t: float) -> Sequence[Any]: ...


class EventCoordinator:
    def __init__(
        self,
        intake: EventIntake,
        pipeline: EventPipeline,
        core: _EventSink,
        *,
        debounce_seconds: float = 120.0,
        audit: Any | None = None,
        holds: HoldLedger | None = None,
        windows: _WindowSource | None = None,
        wall_clock: Callable[[], float] = time.time,
        mono_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._intake = intake
        self._pipeline = pipeline
        self._core = core
        self._window = debounce_seconds
        self._audit = audit
        # Maintenance-window holds. `holds` without `windows` (calendar disabled)
        # still drops replays of held events but holds nothing new; neither (a
        # unit test, or no store) leaves classification out entirely.
        self._holds = holds
        self._windows = windows
        # D4: cadence decisions run on wall-clock (comparable to persisted `at`
        # and stable across a restart); the debounce deadline uses monotonic
        # (a short in-process interval, never persisted).
        self._wall_clock = wall_clock
        self._mono_clock = mono_clock
        self._sleep = sleep
        # --- catch-up state (maintenance-calendar D8) -------------------------
        # On the instance, not locals in run(), so `caught_up` can see them. An
        # event the pump received is always in exactly one place: the queue, the
        # open debounce batch (`_debouncer`, plus `_ready` batches it already
        # closed), or a dispatch (`_dispatch_in_progress`).
        self._queue: "asyncio.Queue[Event]" = asyncio.Queue()
        self._debouncer: Debouncer | None = None
        self._ready: list[list[Event]] = []
        self._dispatch_in_progress = False
        self._keepalive_since_subscribe = False
        self._first_subscribe_at: float | None = None

    # --- catch-up (D8) ----------------------------------------------------------

    def note_subscribe(self) -> None:
        """Intake is about to (re)subscribe: earlier keepalives no longer count."""
        self._keepalive_since_subscribe = False
        if self._first_subscribe_at is None:
            self._first_subscribe_at = self._wall_clock()

    def note_keepalive(self) -> None:
        """Intake received a keepalive frame on the current subscription."""
        self._keepalive_since_subscribe = True

    def note_unsubscribe(self) -> None:
        """The subscription ended: until the next one shows a keepalive, intake is
        not caught up. Without this, a release could fire during the reconnect
        backoff with the reconnect's ``since=`` backlog still unread."""
        self._keepalive_since_subscribe = False

    @property
    def holds(self) -> HoldLedger | None:
        """The hold ledger this coordinator classifies through, if any."""
        return self._holds

    @property
    def first_subscribe_attempt_at(self) -> float | None:
        """This process's first subscribe attempt (wall clock), or None before it."""
        return self._first_subscribe_at

    @property
    def dispatch_in_progress(self) -> bool:
        return self._dispatch_in_progress

    @property
    def caught_up(self) -> bool:
        """Every event intake has received has been dispatched (D8).

        A keepalive on the current subscription (ntfy sends its ``since=`` backlog
        before the first one; cleared when a subscription ends and when the next
        begins), the pump queue empty, no debounce batch open, and no
        batch between leaving the debouncer and its dispatch returning.
        """
        return (
            self._keepalive_since_subscribe
            and self._queue.empty()
            and self._debouncer is None
            and not self._ready
            and not self._dispatch_in_progress
        )

    # --- dispatch -------------------------------------------------------------

    async def dispatch_batch(
        self, batch: Sequence[Event], now: float | None = None
    ) -> None:
        """Hold, evaluate, audit suppressions, submit any turn, and advance the
        durable intake checkpoint only once the batch's outcome is durable (design
        D1) — via the event turn's offset, or a marker for a batch with no turn.

        The hold partition (maintenance-calendar D7) runs first and never returns
        early: held events are durable rows by the time it returns, and duplicates
        need nothing, so a batch with no survivors still reaches the marker branch.
        """
        if now is None:
            now = self._wall_clock()
        # Delivery-order cursor: the last event in the (arrival-ordered) batch.
        offset = batch[-1].id if batch else None

        survivors = self._partition_holds(batch)
        decision = self._pipeline.evaluate(survivors, now=now)
        suppressions_durable = self._write_suppressions(decision.suppressions)

        if decision.event_turn is not None:
            # The core checkpoints `offset` after this triage's record is durable.
            # A mixed batch (turn + suppressions) advances via the triage offset
            # regardless of whether this batch's suppression records persisted:
            # that is safe — a suppressed identity is in cooldown, so replaying it
            # is a no-op, and cadence rehydration reconstructs cooldown from triage
            # records only (never suppressions). The hard guarantee is that no
            # TRIAGEABLE event is skipped, and the core's per-triage flush + latch
            # enforce that. Held events in the batch are already durable rows.
            turn = replace(decision.event_turn, offset=offset)
            await self._core.submit_event(turn)
        elif offset is not None and suppressions_durable:
            # Suppression-only, held-only, duplicate-only or empty-survivor batch:
            # advance past it in FIFO order, but only if every suppression record
            # was persisted. Every held event's row is stored (a row that could not
            # be stored made its event a survivor instead).
            await self._core.submit_marker(CheckpointMarker(offset=offset))

    def _partition_holds(self, batch: Sequence[Event]) -> list[Event]:
        """The events that go on to the pipeline, in batch order (D7).

        1. Duplicate: an id already held is dropped, whatever its window's state.
        2. Publish time: the frame's ``time``, else arrival.
        3. Open window: a window active then that is neither released nor in flight.
        4. Hold against the open window with the latest end; if the row cannot be
           stored, the event goes on (fail open).
        """
        if self._holds is None:
            return list(batch)
        try:
            known = self._holds.known_ids(e.id for e in batch)
        except Exception:
            logger.error(
                "could not read held events; replays of held events in this batch "
                "are processed as new",
                exc_info=True,
            )
            known = set()
        survivors: list[Event] = []
        for event in batch:
            if event.id and event.id in known:
                continue
            if self._windows is None:
                survivors.append(event)
                continue
            published = publish_time(event)
            try:
                active = tuple(self._windows.active(published))
            except Exception:
                logger.error(
                    "could not read maintenance windows; event %s is not held",
                    event.id,
                    exc_info=True,
                )
                active = ()
            result = self._holds.hold(event, published, active)
            if result is HoldResult.NOT_HELD:
                survivors.append(event)
        return survivors

    def _write_suppressions(self, suppressions: Sequence[SuppressionRecord]) -> bool:
        """One ``suppression`` record each; True when every one was persisted."""
        durable = True
        for supp in suppressions:
            if self._audit is not None:
                ok = self._audit.write(
                    suppression_record(
                        identity_key=supp.identity_key,
                        reason=supp.reason,
                        event_id=supp.event_id,
                        at=supp.at,
                    )
                )
                durable = durable and ok
        return durable

    async def submit_release(
        self, events: Sequence[Event], held_release: HeldRelease, now: float
    ) -> bool:
        """Evaluate a release's forwarded events as one batch; True if a turn went out.

        Exactly like a live batch (cooldown, recurrence, the cap, and a
        ``suppression`` record per cooled-down identity), except that the turn
        carries ``held_release`` and **no offset**: the checkpoint advanced past
        these events when they were held. The window is in flight from just before
        the submission until the core's sink marks it released.

        If the submission raises, the window stays in flight: ``evaluate`` has
        already armed the cooldown of every identity in the turn, so a retry in
        this process would suppress them and mark the window released with nothing
        triaged. Unreleased and in flight, it is released again after a restart,
        like a release whose triage record could not be written (D8).
        """
        decision = self._pipeline.evaluate(events, now=now, held_release=held_release)
        self._write_suppressions(decision.suppressions)
        if decision.event_turn is None:
            return False
        turn = replace(decision.event_turn, offset=None)
        if self._holds is not None:
            self._holds.begin_release(
                held_release.window_id, (item.identity.key for item in turn.items)
            )
        await self._core.submit_event(turn)
        return True

    # --- the loop -------------------------------------------------------------

    async def run(self) -> None:
        """Consume events forever, debouncing arrivals into batches."""
        producer = asyncio.create_task(self._pump(self._queue))
        try:
            while True:
                first = await self._queue.get()
                self._debouncer = Debouncer(self._window)
                self._feed(first)
                deadline = self._mono_clock() + self._window
                while True:
                    remaining = deadline - self._mono_clock()
                    if remaining <= 0:
                        break
                    try:
                        event = await asyncio.wait_for(self._queue.get(), remaining)
                    except asyncio.TimeoutError:
                        break
                    self._feed(event)
                # Set where the batch is flushed, before it leaves the instance, and
                # cleared only when its dispatch returns or raises (D8, F2), across
                # every submit await inside dispatch_batch.
                self._dispatch_in_progress = True
                try:
                    for batch in self._flush():
                        try:
                            # now defaults to the wall clock inside dispatch_batch (D4).
                            await self.dispatch_batch(batch)
                        except Exception:
                            logger.exception("failed to dispatch event batch")
                finally:
                    self._dispatch_in_progress = False
        finally:
            producer.cancel()
            try:
                await producer
            except asyncio.CancelledError:
                pass
            except Exception:
                # `await` on an already-FAILED task re-raises its stored exception
                # rather than reporting the cancellation, so a producer that died on
                # its own would surface here and replace the real shutdown reason.
                # Intake normalises its own failures, so this is a backstop.
                logger.exception("event intake producer failed")

    def _feed(self, event: Event) -> None:
        # The arrival-time debouncer closes its batch when an event arrives past
        # the window. That completed batch is kept for dispatch: the loop's own
        # deadline runs on the monotonic clock, and arrival times are wall-clock,
        # so the two can disagree, and dropping the returned batch would lose it.
        completed = self._debouncer.feed(event)  # type: ignore[union-attr]
        if completed:
            self._ready.append(completed)

    def _flush(self) -> list[list[Event]]:
        batches, self._ready = self._ready, []
        last = self._debouncer.flush() if self._debouncer is not None else None
        self._debouncer = None
        if last:
            batches.append(last)
        return batches

    async def _pump(self, queue: "asyncio.Queue[Event]") -> None:
        # Hold the generator instead of consuming it inline: cancelling this task
        # leaves an `async for`'s generator SUSPENDED, not closed — finalised only
        # if the loop ever reaches shutdown_asyncgens(). Without this, intake's own
        # cleanup of the live ntfy connection is decorative on the one production
        # path that has one open.
        #
        # The callbacks run here, in the pump task, after every earlier event was
        # put on the queue (D8): that ordering is what makes `caught_up` sound.
        agen = self._intake.events(
            on_subscribe=self.note_subscribe,
            on_keepalive=self.note_keepalive,
            on_unsubscribe=self.note_unsubscribe,
        )
        try:
            async for event in agen:
                await queue.put(event)
        finally:
            await agen.aclose()
