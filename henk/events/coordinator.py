"""Glue: intake → debounce → pipeline → core, plus suppression audit records.

:meth:`EventCoordinator.dispatch_batch` is the pure decision step (evaluate a
debounced batch, write suppression records, submit any resulting event turn) and
is unit-tested. :meth:`run` wraps it in the async debounce timer that flushes a
quiet batch — deployment wiring, exercised end-to-end at deploy-verify (5.3).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Sequence

from dataclasses import replace

from henk.audit import suppression_record
from henk.agent.turns import CheckpointMarker
from henk.events.intake import EventIntake
from henk.events.pipeline import Debouncer, EventPipeline
from henk.events.types import Event

logger = logging.getLogger("henk.events.coordinator")


class _EventSink:
    async def submit_event(self, turn) -> None: ...

    async def submit_marker(self, marker) -> None: ...


class EventCoordinator:
    def __init__(
        self,
        intake: EventIntake,
        pipeline: EventPipeline,
        core: _EventSink,
        *,
        debounce_seconds: float = 120.0,
        audit: Any | None = None,
        wall_clock: Callable[[], float] = time.time,
        mono_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._intake = intake
        self._pipeline = pipeline
        self._core = core
        self._window = debounce_seconds
        self._audit = audit
        # D4: cadence decisions run on wall-clock (comparable to persisted `at`
        # and stable across a restart); the debounce deadline uses monotonic
        # (a short in-process interval, never persisted).
        self._wall_clock = wall_clock
        self._mono_clock = mono_clock
        self._sleep = sleep

    async def dispatch_batch(
        self, batch: Sequence[Event], now: float | None = None
    ) -> None:
        """Evaluate one debounced batch: audit suppressions, submit any turn, and
        advance the durable intake checkpoint only once the batch's outcome is
        durable (design D1) — via the event turn's offset, or a marker for a
        suppression-only batch."""
        if now is None:
            now = self._wall_clock()
        # Delivery-order cursor: the last event in the (arrival-ordered) batch.
        offset = batch[-1].id if batch else None

        decision = self._pipeline.evaluate(batch, now=now)

        suppressions_durable = True
        for supp in decision.suppressions:
            if self._audit is not None:
                ok = self._audit.write(
                    suppression_record(
                        identity_key=supp.identity_key,
                        reason=supp.reason,
                        event_id=supp.event_id,
                        at=supp.at,
                    )
                )
                suppressions_durable = suppressions_durable and ok

        if decision.event_turn is not None:
            # The core checkpoints `offset` after this triage's record is durable.
            # A mixed batch (turn + suppressions) advances via the triage offset
            # regardless of whether this batch's suppression records persisted:
            # that is safe — a suppressed identity is in cooldown, so replaying it
            # is a no-op, and cadence rehydration reconstructs cooldown from triage
            # records only (never suppressions). The hard guarantee is that no
            # TRIAGEABLE event is skipped, and the core's per-triage flush + latch
            # enforce that.
            turn = replace(decision.event_turn, offset=offset)
            await self._core.submit_event(turn)
        elif offset is not None and suppressions_durable:
            # Suppression-only (or empty-survivor) batch: advance past it in FIFO
            # order, but only if every suppression record was persisted.
            await self._core.submit_marker(CheckpointMarker(offset=offset))

    async def run(self) -> None:  # pragma: no cover - async debounce timing (deploy)
        """Consume events forever, debouncing arrivals into batches."""
        queue: "asyncio.Queue[Event]" = asyncio.Queue()
        producer = asyncio.create_task(self._pump(queue))
        try:
            while True:
                first = await queue.get()
                debouncer = Debouncer(self._window)
                debouncer.feed(first)
                deadline = self._mono_clock() + self._window
                while True:
                    remaining = deadline - self._mono_clock()
                    if remaining <= 0:
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), remaining)
                    except asyncio.TimeoutError:
                        break
                    debouncer.feed(event)
                batch = debouncer.flush()
                if batch:
                    try:
                        # now defaults to the wall clock inside dispatch_batch (D4).
                        await self.dispatch_batch(batch)
                    except Exception:
                        logger.exception("failed to dispatch event batch")
        finally:
            producer.cancel()
            try:
                await producer
            except asyncio.CancelledError:
                pass

    async def _pump(self, queue: "asyncio.Queue[Event]") -> None:  # pragma: no cover
        async for event in self._intake.events():
            await queue.put(event)
