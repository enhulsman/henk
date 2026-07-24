"""Typed turns carried by the agent core's single serial queue (agent-core delta).

v1 queued bare strings. v1.2 distinguishes an **owner turn** (an inbound Signal
message — reply path, no triage framing) from an **event turn** (a debounced,
cooldown-surviving incident — proactive path, triage framing). Keeping both in
one queue preserves the single serial concurrency model: an event turn and an
owner turn never run at once (design D5).

The turn types live here (the core owns the turn taxonomy); the event pipeline
constructs :class:`EventTurn` values and the core consumes them.
"""

from __future__ import annotations

from dataclasses import dataclass

from henk.events.types import AlertIdentity, Event


@dataclass(frozen=True)
class OwnerTurn:
    """An inbound owner message. Runs on the reply path with no triage framing."""

    text: str


@dataclass(frozen=True)
class EventTurnItem:
    """One incident inside an event turn (a storm collapses several of these)."""

    event: Event
    identity: AlertIdentity
    recurrence: bool = False
    prior_handoff_ref: str | None = None


@dataclass(frozen=True)
class EventTurn:
    """A debounced batch of triageable incidents to triage in one session.

    ``announceable`` gates only the proactive Signal send — a non-announceable
    (cap-overflow) event turn still runs its full triage session and publishes a
    handoff (incident-triage spec). ``suppressed_count`` is the number of
    cap-suppressed incidents accumulated since the last announceable message,
    surfaced in this message when it is announceable. ``offset`` is the batch's
    last-seen ntfy message id: the core advances the durable intake checkpoint to
    it only after this triage's audit record is written (design D1).
    """

    items: tuple[EventTurnItem, ...]
    announceable: bool = True
    suppressed_count: int = 0
    offset: str | None = None


@dataclass(frozen=True)
class CheckpointMarker:
    """A suppression-only batch's checkpoint advance, riding the serial queue.

    When a debounced batch produces no triage turn (every event was cooled down),
    the intake checkpoint still has to advance past those events — but only after
    any in-flight triage ahead of it in the queue has flushed. Routing the advance
    through the same FIFO core queue as a marker preserves that ordering (design
    D1): the offset never advances past an event whose outcome isn't yet durable.
    """

    offset: str


Turn = OwnerTurn | EventTurn | CheckpointMarker
