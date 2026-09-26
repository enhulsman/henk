"""Bounded, best-effort owner acknowledgement: the read receipt and working indicator.

The channel adapter's acknowledgement operations (``acknowledge``,
``start_working``, ``stop_working``) are single, unbounded attempts that report
``True``/``False`` and never log. This module is what makes them safe to call
from the receive loop and from an owner turn (owner-acknowledgement D3-D6):

- **the bound.** Every operation is bounded as a whole by the acknowledge
  timeout, enforced by cancelling it with ``asyncio.timeout``. A transport's own
  per-phase timeouts bound each socket operation, never a whole request, so
  cancellation is the only whole-request bound there is. Cancelling is safe here
  and not for sends because an acknowledgement carries no content and is
  idempotent: nothing can be duplicated or lost that matters.
- **the working-indicator bracket** ``working()``: one task issues every typing
  request of a turn, sequentially, so a start can never land after the stop.
- **the log line**, once per turn, since only this layer knows what a turn is.

**Why the bound lives here and not in the adapter.** The Signal adapter's guard
test forbids any ``timeout``/``wait_for`` call in that module, deliberately, so
that a hold timer on its send lock has to argue with the decision. Admitting an
acknowledgement timeout there would weaken a guard that protects something
else. So the adapter stays unbounded, and this channel-neutral module (it knows
no wire format and no identity) carries the bound for every adapter.

Everything here catches ``Exception``, never ``BaseException``: a cancellation of
the caller, as at shutdown, always propagates.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

from henk.channel.base import ChannelAdapter

logger = logging.getLogger("henk.channel.acknowledge")

#: How often the indicator task re-reads the pause predicate while a turn runs.
#: A wake-up per second during a turn; it never delays the close, which wakes the
#: task through its exit event instead (design D5).
PAUSE_POLL_SECONDS = 1.0


@dataclass
class _TurnLog:
    """The per-turn log budget: one line for the loop, at most one for the close."""

    loop_logged: bool = False
    close_logged: bool = False


def _describe(exc: BaseException | None) -> str:
    if exc is None:
        return "not accepted"
    if isinstance(exc, TimeoutError):
        return "timed out"
    return repr(exc)


class OwnerAcknowledgement:
    """Bounded receipts and a bounded working-indicator bracket over one adapter.

    ``timeout`` bounds each operation as a whole; ``refresh_seconds`` is the
    channel's re-assert interval; ``paused`` (e.g. the approval gate's
    ``has_pending``) suspends the indicator while true. ``sleep`` and ``clock``
    are injectable together: an injected sleep without an injected clock could
    not test that an interval has elapsed.
    """

    def __init__(
        self,
        adapter: ChannelAdapter,
        *,
        timeout: float,
        refresh_seconds: float,
        paused: Callable[[], bool] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._timeout = timeout
        self._refresh = refresh_seconds
        self._paused = paused if paused is not None else (lambda: False)
        self._sleep = sleep
        self._clock = clock

    # --- Read receipt ------------------------------------------------------

    async def receipt(self, channel_ref: str | None) -> None:
        """Send the owner a read receipt, bounded; never raises except on cancel.

        A message without a reference is a no-op: no request, nothing logged.
        """
        if channel_ref is None:
            return
        failure: BaseException | None = None
        try:
            async with asyncio.timeout(self._timeout):
                accepted = await self._adapter.acknowledge(channel_ref)
        except Exception as exc:  # noqa: BLE001 - best-effort; TimeoutError included
            accepted, failure = False, exc
        if not accepted:
            logger.warning(
                "owner read receipt for ref=%s failed (%s)",
                channel_ref,
                _describe(failure),
            )

    # --- Working indicator -------------------------------------------------

    @asynccontextmanager
    async def working(self) -> AsyncIterator[None]:
        """Show the working indicator for the body's duration (design D3/D4).

        Entering creates the indicator task and yields once, so its first start
        is issued before the body runs; the body never waits on that start's
        response. A normal or ``Exception`` exit closes the indicator within one
        bound. A cancellation (or any other ``BaseException``) cancels the task
        without a stop and propagates.
        """
        exit_event = asyncio.Event()
        log = _TurnLog()
        task = asyncio.create_task(self._indicate(exit_event, log))
        try:
            # Inside the try: a cancellation delivered at this yield must still
            # reach the task, or it would outlive the turn.
            await asyncio.sleep(0)
            yield
        except Exception:
            await self._close(exit_event, task, log)
            raise
        except BaseException:
            # Shutdown: no stop over the network. A fresh request inside a
            # cancelled turn is not reliably completable and would sit on the
            # shutdown path; the client-side expiry clears the indicator (D4).
            task.cancel()
            await asyncio.wait({task})
            raise
        await self._close(exit_event, task, log)

    async def _close(
        self, exit_event: asyncio.Event, task: asyncio.Task, log: _TurnLog
    ) -> None:
        """Wake the task and let it finish, within ONE bound; never raises on expiry.

        Nesting matters. The ``BaseException`` wrapper sits inside the timeout, so
        an expiry (delivered as a cancellation) or an outer cancel both cancel the
        task and wait for it before propagating. The ``except Exception`` sits
        outside, so an expiry surfaces as ``TimeoutError`` and is logged, while an
        outer ``CancelledError`` passes through. The task is awaited with
        ``asyncio.wait``, never ``await task``: awaiting the task directly would
        forward an outer cancel into it and let an ``except CancelledError``
        swallow the caller's own shutdown.
        """
        try:
            async with asyncio.timeout(self._timeout):
                exit_event.set()
                try:
                    await asyncio.wait({task})
                except BaseException:
                    task.cancel()
                    await asyncio.wait({task})
                    raise
        except Exception as exc:  # noqa: BLE001 - best-effort; TimeoutError included
            self._log_close(log, exc)
            return
        if not task.cancelled() and task.exception() is not None:
            self._log_close(log, task.exception())

    async def _indicate(self, exit_event: asyncio.Event, log: _TurnLog) -> None:
        """The one task that issues every typing request of a turn (design D4/D5)."""
        next_due = await self._start(log)
        paused = False
        while not exit_event.is_set():
            await self._wake(
                exit_event,
                max(0.0, min(PAUSE_POLL_SECONDS, next_due - self._clock())),
            )
            if exit_event.is_set():
                break
            if self._paused():
                if not paused:
                    paused = True
                    # Nothing is due while paused, so the wait above is the poll.
                    next_due = math.inf
                    await self._attempt(self._adapter.stop_working, log, "pause stop")
            elif paused:
                paused = False
                next_due = await self._start(log, "resume start")
            elif self._clock() >= next_due:
                next_due = await self._start(log, "refresh")
        # Only on this normal exit path; bounded by the close as a whole. Sent even
        # when paused: a stop is idempotent, and the pause stop may have failed.
        try:
            accepted = await self._adapter.stop_working()
        except Exception as exc:  # noqa: BLE001 - best-effort
            self._log_close(log, exc)
            return
        if not accepted:
            self._log_close(log, None)

    async def _start(self, log: _TurnLog, what: str = "start") -> float:
        """Issue a start; the next one is due an interval after this ISSUE time.

        Measured from issue, whether or not the start succeeded, so a refresh that
        hangs until its bound is followed at once by the overdue one (design D4).
        """
        issued = self._clock()
        await self._attempt(self._adapter.start_working, log, what)
        return issued + self._refresh

    async def _attempt(
        self, operation: Callable[[], Awaitable[bool]], log: _TurnLog, what: str
    ) -> None:
        """One bounded start, refresh or pause stop, logged once per turn."""
        failure: BaseException | None = None
        try:
            async with asyncio.timeout(self._timeout):
                accepted = await operation()
        except Exception as exc:  # noqa: BLE001 - best-effort; TimeoutError included
            accepted, failure = False, exc
        if not accepted and not log.loop_logged:
            log.loop_logged = True
            logger.warning(
                "owner working indicator %s failed (%s); "
                "further start/refresh failures this turn are not logged",
                what,
                _describe(failure),
            )

    async def _wake(self, exit_event: asyncio.Event, delay: float) -> None:
        """Wait for ``exit_event`` or ``delay``, whichever comes first.

        Two tasks, since ``asyncio.wait`` refuses bare coroutines. Both are
        cancelled and awaited on every path, a cancellation of this task
        included, so neither outlives the wake-up.
        """
        waiters = {
            asyncio.create_task(exit_event.wait()),
            asyncio.create_task(self._sleep(delay)),
        }
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.wait(waiters)

    def _log_close(self, log: _TurnLog, failure: BaseException | None) -> None:
        if log.close_logged:
            return
        log.close_logged = True
        logger.warning(
            "owner working indicator close failed (%s); "
            "leaving it to the client-side expiry",
            _describe(failure),
        )
