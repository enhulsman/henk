"""Debounce → cooldown → recurrence → cadence-cap policy (design D6).

Three layers keep Signal quiet, plus recurrence framing:

1. :class:`Debouncer` collapses events arriving within one *arrival-time* window
   into a single batch — so an alert storm, or a replayed backlog after a
   reconnect, becomes one conversation (event-intake spec).
2. Per-identity **cooldown** (config-driven, with per-pattern overrides so a
   chronic identity like swap pressure can carry 24h): a re-fire inside cooldown
   never starts a conversation but is still recorded (a ``SuppressionRecord``).
3. A **recurrence window** wider than cooldown: a re-fire that survives cooldown
   but was triaged recently is flagged so triage stays brief and points at the
   prior handoff instead of re-gathering evidence.
4. A daily **cadence cap** that gates the proactive Signal send ONLY — a
   cap-overflow incident still triages, hands off, and is audited; the count of
   suppressed incidents surfaces on the next announceable message.

:class:`EventPipeline` is pure policy over hand-built batches with an explicit
``now`` (no real time), so all of it is unit-tested; the async debounce timer
that flushes a quiet batch is deployment wiring in the coordinator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from henk.agent.turns import EventTurn, EventTurnItem
from henk.events.identity import derive_identity
from henk.events.types import Event

DAY_SECONDS = 24 * 3600.0


@dataclass(frozen=True)
class PipelineConfig:
    debounce_seconds: float = 120.0
    cooldown_seconds: float = 6 * 3600.0
    recurrence_window_seconds: float = DAY_SECONDS
    cap_per_24h: int = 3
    cap_window_seconds: float = DAY_SECONDS
    #: Each entry: {"pattern": <regex, matched case-insensitively against the
    #: identity key>, "cooldown_seconds": <override>}. First match wins.
    cooldown_overrides: Sequence[Mapping[str, Any]] = field(default_factory=tuple)


@dataclass(frozen=True)
class SuppressionRecord:
    """A suppressed event that gets an audit record but no conversation."""

    identity_key: str
    reason: str  # "cooldown"
    event_id: str
    at: float


@dataclass(frozen=True)
class BatchDecision:
    event_turn: EventTurn | None
    suppressions: list[SuppressionRecord] = field(default_factory=list)


class Debouncer:
    """Arrival-time debouncer: groups events within ``window`` into one batch."""

    def __init__(self, window: float) -> None:
        self._window = window
        self._batch: list[Event] = []
        self._batch_start: float | None = None

    def feed(self, event: Event) -> list[Event] | None:
        """Buffer ``event``; return a completed batch if this event opens a new one."""
        if self._batch_start is None:
            self._batch_start = event.arrival_time
            self._batch = [event]
            return None
        if event.arrival_time - self._batch_start <= self._window:
            self._batch.append(event)
            return None
        # This event arrived past the window — the open batch is complete.
        completed = self._batch
        self._batch = [event]
        self._batch_start = event.arrival_time
        return completed

    def flush(self) -> list[Event] | None:
        """Return and clear any buffered batch (window elapsed / end of stream)."""
        if not self._batch:
            return None
        completed = self._batch
        self._batch = []
        self._batch_start = None
        return completed


class EventPipeline:
    """Applies cooldown, recurrence, and the cadence cap to debounced batches."""

    def __init__(self, config: PipelineConfig) -> None:
        self._cfg = config
        self._last_triaged: dict[str, float] = {}
        self._last_handoff_ref: dict[str, str] = {}
        self._announce_times: list[float] = []
        self._cap_suppressed_since_announce = 0

    def note_handoff(self, identity_key: str, ref: str) -> None:
        """Record the handoff id a triage produced, for later recurrence framing."""
        if ref:
            self._last_handoff_ref[identity_key] = ref

    def evaluate(self, batch: Sequence[Event], now: float) -> BatchDecision:
        suppressions: list[SuppressionRecord] = []
        survivors: list[EventTurnItem] = []
        seen: set[str] = set()

        for event in batch:
            ident = derive_identity(event)
            if ident.key in seen:
                continue  # dedup within the batch — one item per identity
            seen.add(ident.key)

            last = self._last_triaged.get(ident.key)
            cooldown = self._cooldown_for(ident.key)
            if last is not None and (now - last) < cooldown:
                suppressions.append(
                    SuppressionRecord(
                        identity_key=ident.key,
                        reason="cooldown",
                        event_id=event.id,
                        at=now,
                    )
                )
                continue

            recurrence = (
                last is not None
                and (now - last) < self._cfg.recurrence_window_seconds
            )
            survivors.append(
                EventTurnItem(
                    event=event,
                    identity=ident,
                    recurrence=recurrence,
                    prior_handoff_ref=(
                        self._last_handoff_ref.get(ident.key) if recurrence else None
                    ),
                )
            )

        if not survivors:
            return BatchDecision(event_turn=None, suppressions=suppressions)

        for item in survivors:
            self._last_triaged[item.identity.key] = now

        announceable, suppressed_count = self._apply_cap(now)
        turn = EventTurn(
            items=tuple(survivors),
            announceable=announceable,
            suppressed_count=suppressed_count,
        )
        return BatchDecision(event_turn=turn, suppressions=suppressions)

    def _cooldown_for(self, key: str) -> float:
        for override in self._cfg.cooldown_overrides:
            pattern = override.get("pattern")
            if pattern and re.search(pattern, key, re.IGNORECASE):
                return float(override["cooldown_seconds"])
        return self._cfg.cooldown_seconds

    def _apply_cap(self, now: float) -> tuple[bool, int]:
        self._announce_times = [
            t for t in self._announce_times if now - t < self._cfg.cap_window_seconds
        ]
        if len(self._announce_times) < self._cfg.cap_per_24h:
            self._announce_times.append(now)
            surfaced = self._cap_suppressed_since_announce
            self._cap_suppressed_since_announce = 0
            return True, surfaced
        self._cap_suppressed_since_announce += 1
        return False, 0
