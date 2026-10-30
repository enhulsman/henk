"""Holding events during maintenance windows, and marking windows released.

Design D7 (hold at dispatch) and the release-side half of D8 (marked released at
durable flush). :class:`HoldLedger` is the one object both the coordinator and the
core's release sink talk to, so it can be built before either: it owns the
repository, the ``hold``/``hold-release`` receipts and the in-flight set.

- :meth:`HoldLedger.hold` decides one event's fate against the windows active at its
  publish time: held, a duplicate, or not held (no open window, or the row could not
  be stored, which fails open).
- :meth:`HoldLedger.mark_released` is the release sink (group 7 wires the core to
  it): it sets ``released_at`` once, writes the window's single ``hold-release``
  record and takes the window out of the in-flight set.

A window is **open** unless it has been released or its release is in flight
(event-intake delta). Neither this module nor the repository logs a title or a
message: log lines carry ids and identity keys, as the audit records do.
"""

from __future__ import annotations

import enum
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol, Sequence

from henk.events.identity import derive_identity
from henk.events.types import Event, EventState
from henk.store.holds import DUPLICATE, HELD, HeldEvent, HoldStore

logger = logging.getLogger("henk.events.holds")


class _Window(Protocol):
    id: str
    title: str
    start: float
    end: float


class HoldResult(enum.Enum):
    """What :meth:`HoldLedger.hold` did with one event."""

    HELD = "held"  # stored; the event goes no further
    DUPLICATE = "duplicate"  # already held; dropped, no record
    NOT_HELD = "not-held"  # no open window, or the row could not be stored


def publish_time(event: Event) -> float:
    """When ``event`` was published: the ntfy frame's ``time``, else its arrival."""
    return event.published_at if event.published_at is not None else event.arrival_time


@dataclass(frozen=True)
class HeldGroups:
    """A window's held rows grouped by identity (design D8, release step 1).

    ``held`` and ``recovered`` count distinct identities. ``forwarded`` is one row
    per identity that did not recover, its latest non-resolved event, in publish
    order.
    """

    held: int
    recovered: int
    forwarded: tuple[HeldEvent, ...]


def group_held(rows: Sequence[HeldEvent]) -> HeldGroups:
    """Group rows (in publish order) by identity: recovered, or forwarded.

    An identity whose latest held event is ``resolved`` recovered on its own.
    Every other identity is forwarded with its latest non-resolved event, which is
    simply its latest event: an identity is forwarded only because that one is not
    ``resolved``.
    """
    latest: dict[str, HeldEvent] = {}
    for row in rows:
        latest[row.identity_key] = row
    resolved = EventState.RESOLVED.value
    recovered = sum(1 for row in latest.values() if row.state == resolved)
    order = {row.event_id: i for i, row in enumerate(rows)}
    forwarded = sorted(
        (row for row in latest.values() if row.state != resolved),
        key=lambda r: order[r.event_id],
    )
    return HeldGroups(held=len(latest), recovered=recovered, forwarded=tuple(forwarded))


def rebuild_event(row: HeldEvent) -> Event:
    """The held event as intake delivered it, from its stored fields."""
    try:
        raw = json.loads(row.raw_json)
    except ValueError:
        raw = {}
    return Event(
        id=row.event_id,
        title=row.title,
        message=row.message,
        arrival_time=row.arrival_time,
        raw=raw if isinstance(raw, dict) else {},
        published_at=row.published_at,
    )


class HoldLedger:
    """Holds events into open windows and marks windows released. Never raises
    from :meth:`hold` or :meth:`mark_released`.

    ``receipts`` is a :class:`henk.audit.logger.HoldReceipts`; its methods return
    whether the record was written. ``clock`` is the wall clock (``held_at``,
    ``released_at``).
    """

    def __init__(
        self,
        store: HoldStore,
        receipts: Any,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._receipts = receipts
        self._clock = clock
        # Windows whose release turn has been submitted and whose triage record is
        # not yet durable. Process-local on purpose: after a restart such a window
        # is unreleased and is released again (D8: a duplicate triage over a lost
        # one).
        self._in_flight: set[str] = set()

    @property
    def store(self) -> HoldStore:
        return self._store

    @property
    def in_flight(self) -> frozenset[str]:
        return frozenset(self._in_flight)

    # --- holding (D7) ---------------------------------------------------------

    def known_ids(self, event_ids: Iterable[str]) -> set[str]:
        """Event ids already held: the duplicate check. Raises StoreError."""
        return self._store.known_event_ids(event_ids)

    def hold(
        self, event: Event, published_at: float, active: Sequence[_Window]
    ) -> HoldResult:
        """Hold ``event`` against the open window with the latest end, if any.

        ``active`` are the windows active at ``published_at``. Closed ones
        (released, or release in flight) are skipped. If the held row cannot be
        stored the event is NOT_HELD, so it goes to the pipeline as if no window
        existed (fail open). A ``hold`` record failure is logged and the hold
        stands: the row is the durable fact.
        """
        if not event.id or not active:
            return HoldResult.NOT_HELD
        try:
            stored = self._store.windows(w.id for w in active)
            open_windows = [
                w
                for w in active
                if w.id not in self._in_flight
                and (w.id not in stored or stored[w.id].released_at is None)
            ]
            if not open_windows:
                return HoldResult.NOT_HELD
            window = max(open_windows, key=lambda w: (w.end, w.start, w.id))
            identity = derive_identity(event)
            result = self._store.hold(
                window_id=window.id,
                window_title=window.title,
                window_start=window.start,
                window_end=window.end,
                event_id=event.id,
                identity_key=identity.key,
                state=identity.state.value,
                title=event.title,
                message=event.message,
                raw_json=json.dumps(dict(event.raw), ensure_ascii=False),
                published_at=published_at,
                arrival_time=event.arrival_time,
                held_at=self._clock(),
            )
        except Exception:
            logger.error(
                "could not store the hold for event %s; processing it as if no "
                "maintenance window were open",
                event.id,
                exc_info=True,
            )
            return HoldResult.NOT_HELD
        if result.status == DUPLICATE:
            return HoldResult.DUPLICATE
        if result.status != HELD:  # CLOSED: released since the windows were read
            return HoldResult.NOT_HELD
        logger.info(
            "held event %s (%s) in maintenance window %s",
            event.id,
            identity.key,
            window.id,
        )
        written = self._receipts.held(
            identity_key=identity.key,
            state=identity.state.value,
            event_id=event.id,
            window_id=window.id,
            window_end=result.window_end,
            published_at=published_at,
        )
        if not written:
            logger.error(
                "the hold record for event %s in window %s was not written; the hold "
                "stands (the held row is stored)",
                event.id,
                window.id,
            )
        return HoldResult.HELD

    # --- releasing (D8) -------------------------------------------------------

    def begin_release(self, window_id: str) -> None:
        """The window's release turn is being submitted: closed to new holds, and
        not released again by this process until :meth:`mark_released`."""
        self._in_flight.add(window_id)

    def mark_released(self, window_id: str) -> bool:
        """Mark the window released and write its one ``hold-release`` record.

        The release sink. The core calls it once the release triage's audit record
        is durable (or at once when audit is not configured), and the release calls
        it directly when it produced no turn. True when this call marked it.

        Guarded on ``released_at IS NULL`` in the store, so a second call writes
        nothing. A store failure leaves the window unreleased AND in flight, so this
        process does not release it again; the next restart does.
        """
        try:
            rows = self._store.mark_released(window_id, at=self._clock())
        except Exception:
            logger.error(
                "could not mark maintenance window %s released; it stays in flight "
                "and is released again after a restart",
                window_id,
                exc_info=True,
            )
            return False
        self._in_flight.discard(window_id)
        if rows is None:
            return False
        groups = group_held(rows)
        written = self._receipts.released(
            window_id=window_id,
            held=groups.held,
            recovered=groups.recovered,
            forwarded=groups.held - groups.recovered,
        )
        logger.info(
            "released maintenance window %s: %d held, %d recovered",
            window_id,
            groups.held,
            groups.recovered,
        )
        if not written:
            logger.error(
                "the hold-release record for window %s was not written; the window "
                "is released",
                window_id,
            )
        return True
