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

from henk.audit import suppression_record
from henk.events.intake import EventIntake
from henk.events.pipeline import Debouncer, EventPipeline
from henk.events.types import Event

logger = logging.getLogger("henk.events.coordinator")


class _EventSink:
    async def submit_event(self, turn) -> None: ...


class EventCoordinator:
    def __init__(
        self,
        intake: EventIntake,
        pipeline: EventPipeline,
        core: _EventSink,
        *,
        debounce_seconds: float = 120.0,
        audit: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._intake = intake
        self._pipeline = pipeline
        self._core = core
        self._window = debounce_seconds
        self._audit = audit
        self._clock = clock
        self._sleep = sleep

    async def dispatch_batch(self, batch: Sequence[Event], now: float) -> None:
        """Evaluate one debounced batch: audit suppressions, submit any turn."""
        decision = self._pipeline.evaluate(batch, now=now)
        for supp in decision.suppressions:
            if self._audit is not None:
                self._audit.write(
                    suppression_record(
                        identity_key=supp.identity_key,
                        reason=supp.reason,
                        event_id=supp.event_id,
                        at=supp.at,
                    )
                )
        if decision.event_turn is not None:
            await self._core.submit_event(decision.event_turn)

    async def run(self) -> None:  # pragma: no cover - async debounce timing (deploy)
        """Consume events forever, debouncing arrivals into batches."""
        queue: "asyncio.Queue[Event]" = asyncio.Queue()
        producer = asyncio.create_task(self._pump(queue))
        try:
            while True:
                first = await queue.get()
                debouncer = Debouncer(self._window)
                debouncer.feed(first)
                deadline = self._clock() + self._window
                while True:
                    remaining = deadline - self._clock()
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
                        await self.dispatch_batch(batch, now=self._clock())
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
