"""Core event value types shared across intake, identity, and the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class EventState(str, Enum):
    """Firing-vs-resolved lifecycle state of an alert, per the payload contract."""

    FIRING = "firing"
    RESOLVED = "resolved"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Event:
    """One event as received over the ntfy subscription.

    ``raw`` keeps the verbatim ntfy JSON object so nothing captured by a sensor
    is lost before triage; ``arrival_time`` is stamped by the intake on receipt
    (NOT the sensor's original timestamp) because debounce is measured on arrival
    so replayed backlogs collapse into one catch-up turn (event-intake spec).
    """

    id: str
    title: str
    message: str
    arrival_time: float
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertIdentity:
    """Stable identity derived from an event's contract fields.

    ``key`` is the single value used for dedup, cooldown, and recurrence: the
    same alert re-firing MUST derive the same key, and a nonconforming event
    MUST still receive a deterministic fallback key (event-intake spec).
    """

    key: str
    source: str
    name: str
    state: EventState
