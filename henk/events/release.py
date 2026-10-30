"""Releasing held events after their window (maintenance-calendar D8).

:class:`HoldReleaser` is a background task: it evaluates release at startup and
then every :data:`RELEASE_TICK_SECONDS`, whenever any window has unreleased held
events, **whether or not the calendar is enabled** (with no window source the
extension check is skipped). Each tick, for each unreleased window not in flight:

1. **Due**: its stored (frozen) end plus the grace has passed.
2. **Extension** (only with a window source): if the current snapshot has the same
   window id, active now, starting at or before the earliest held publish time,
   the stored end moves to the calendar's end and the release is deferred. A
   window moved elsewhere is not active now, so it is released at its stored end.
3. **Caught up**: the coordinator has dispatched everything intake received since
   a keepalive on the latest subscription, or :data:`CATCH_UP_FALLBACK_SECONDS`
   have passed since the later of the due instant and this process's first
   subscribe attempt (so an unreachable ntfy cannot hold alerts forever, and a
   long outage cannot skip the catch-up).
4. **Release**: group the rows by identity. A recovered identity (latest held event
   ``resolved``) is counted; every other one is forwarded with its latest
   non-resolved event, through :meth:`EventCoordinator.submit_release` as one batch
   with no offset. A resulting turn carries a :class:`HeldRelease`, and the core
   marks the window released once that triage's record is durable. A release with
   no turn (all recovered, or every forwarded identity cooled down) marks itself
   at once.

A window that holds an identity an in-flight release is triaging waits until that
release is marked (Codex review): that identity's cooldown is armed only in memory
until then, and must not close this window for good if the process dies first.
Windows sharing no identity are not held up by it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Protocol

from henk.agent.turns import HeldRelease
from henk.events.coordinator import EventCoordinator
from henk.events.holds import HoldLedger, group_held, rebuild_event
from henk.store.holds import HoldWindow

logger = logging.getLogger("henk.events.release")

#: Release is evaluated at least this often (event-intake delta: every 60 s).
RELEASE_TICK_SECONDS = 60.0
#: Without a keepalive, a due window is released this long after the later of its
#: due instant and this process's first subscribe attempt (D8).
CATCH_UP_FALLBACK_SECONDS = 15 * 60.0


class _WindowSource(Protocol):
    """The part of :class:`henk.calendar.windows.MaintenanceWindows` used here."""

    def active_window(self, window_id: str, t: float) -> Any: ...


class HoldReleaser:
    """The release task. One per runtime whenever events are enabled.

    ``grace_seconds`` is ``calendar.grace_minutes * 60``, read even when the
    calendar is disabled (the section's defaults apply then). ``windows`` is the
    :class:`MaintenanceWindows` provider, or None when the calendar is disabled.
    """

    def __init__(
        self,
        ledger: HoldLedger,
        coordinator: EventCoordinator,
        *,
        grace_seconds: float,
        windows: _WindowSource | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        tick_seconds: float = RELEASE_TICK_SECONDS,
        fallback_seconds: float = CATCH_UP_FALLBACK_SECONDS,
    ) -> None:
        if coordinator.holds is not ledger:
            # The coordinator closes in-flight windows to new holds, and the release
            # skips them, through ONE in-flight set. Two ledgers would release the
            # same window on every tick.
            raise ValueError(
                "the releaser and the coordinator must share one HoldLedger"
            )
        self._ledger = ledger
        self._coordinator = coordinator
        self._grace = float(grace_seconds)
        self._windows = windows
        self._clock = clock
        self._sleep = sleep
        self._tick_seconds = float(tick_seconds)
        self._fallback = float(fallback_seconds)

    async def run(self) -> None:
        """Tick at once, then every ``tick_seconds``. Only cancellation stops it."""
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("maintenance-window release tick failed")
            await self._sleep(self._tick_seconds)

    async def tick(self, now: float | None = None) -> None:
        """Evaluate every unreleased window once. One window's failure is logged
        and does not stop the others."""
        if now is None:
            now = self._clock()
        windows = self._ledger.store.unreleased_windows()
        for window in windows:
            if window.window_id in self._ledger.in_flight:
                continue
            try:
                await self._consider(window, now)
            except Exception:
                logger.exception(
                    "could not release maintenance window %s; trying again next tick",
                    window.window_id,
                )

    async def _consider(self, window: HoldWindow, now: float) -> None:
        due = window.end + self._grace
        if now < due:
            return
        rows = self._ledger.store.held_events(window.window_id)
        if not rows:
            return
        if self._extended(window, rows, now):
            return
        if not (self._coordinator.caught_up or self._fallback_due(due, now)):
            return
        shared = self._ledger.in_flight_identities() & {r.identity_key for r in rows}
        if shared:
            # Another window's release is triaging one of these identities and its
            # record is not durable yet; releasing now would meet a cooldown armed
            # only in memory and could mark this window released with its alert
            # untriaged, for good, if the process dies before that flush.
            logger.info(
                "maintenance window %s waits for an in-flight release of %s",
                window.window_id,
                ", ".join(sorted(shared)),
            )
            return
        groups = group_held(rows)
        release = HeldRelease(
            window_id=window.window_id,
            title=window.title,
            start=window.start,
            end_with_grace=due,
            held=groups.held,
            recovered=groups.recovered,
            hold_age_seconds=now - window.end,
        )
        submitted = False
        if groups.forwarded:
            submitted = await self._coordinator.submit_release(
                [rebuild_event(row) for row in groups.forwarded], release, now
            )
        if not submitted:
            self._ledger.mark_released(window.window_id)

    def _extended(self, window: HoldWindow, rows, now: float) -> bool:
        """D8's extension rule; moves the stored end when it applies."""
        if self._windows is None:
            return False
        current = self._windows.active_window(window.window_id, now)
        if current is None:
            return False
        earliest = min(row.published_at for row in rows)
        if current.start > earliest:
            return False
        if current.end != window.end:
            self._ledger.store.extend(window.window_id, current.end)
            logger.info(
                "maintenance window %s was extended; release deferred", window.window_id
            )
        return True

    def _fallback_due(self, due: float, now: float) -> bool:
        first = self._coordinator.first_subscribe_attempt_at
        if first is None:
            return False
        return max(due, first) + self._fallback <= now
