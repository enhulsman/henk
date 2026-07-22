"""Per-source stable-identity derivation with a normalized-title fallback.

The real sensors do NOT follow the idealized ``source | name | state`` title
(verified during infra prep 1.3/1.4 — Grafana emits ``[FIRING:n] {alertname}``
and Gatus emits ``Gatus: {group}/{endpoint}`` with state in the body). So the
derivation is a small ordered set of per-source rules keyed on each sensor's
actual format, with a deterministic normalized-title fallback for anything that
matches none of them (event-intake spec: "nonconforming event still keyed").

The identity ``key`` is state-independent on purpose: a fire and its later
resolve for the same alert share one key so cooldown/recurrence track the
*incident*, not the transition.
"""

from __future__ import annotations

import re

from henk.events.types import AlertIdentity, Event, EventState

# Grafana's ntfy template prefixes the title with a bracketed state marker,
# usually behind an emoji: "🚨 [FIRING:1] {alertname} {folder} (...)".
_GRAFANA_STATE = re.compile(r"\[(FIRING|RESOLVED)", re.IGNORECASE)
_GRAFANA_ALERTNAME = re.compile(r"alertname\s*=\s*(\S+)")
_GATUS_PREFIX = "Gatus:"
_WHITESPACE = re.compile(r"\s+")


def normalized_title(title: str) -> str:
    """Deterministic fallback key body: lowercased, whitespace-collapsed title."""
    return _WHITESPACE.sub(" ", title.strip().lower())


def _gatus_state(message: str) -> EventState:
    lowered = message.lower()
    if "has been resolved" in lowered or "resolved" in lowered:
        return EventState.RESOLVED
    if "has been triggered" in lowered or "triggered" in lowered:
        return EventState.FIRING
    return EventState.UNKNOWN


def _derive_gatus_native(event: Event) -> AlertIdentity:
    # "Gatus: {group}/{endpoint}" — the name is everything after the prefix.
    name = event.title[len(_GATUS_PREFIX) :].strip()
    return AlertIdentity(
        key=f"gatus:{name}",
        source="gatus",
        name=name,
        state=_gatus_state(event.message),
    )


def _derive_grafana(event: Event, marker: re.Match[str]) -> AlertIdentity:
    state = (
        EventState.FIRING
        if marker.group(1).upper() == "FIRING"
        else EventState.RESOLVED
    )
    # Prefer the explicit label; fall back to the first token after the marker.
    label = _GRAFANA_ALERTNAME.search(event.message)
    if label:
        name = label.group(1)
    else:
        tail = event.title[marker.end() :]
        # Skip the "]" / ":n]" remainder of the state marker, then take a token.
        tail = tail.split("]", 1)[-1].strip()
        name = tail.split()[0] if tail.split() else normalized_title(event.title)
    return AlertIdentity(
        key=f"grafana:{name}", source="grafana", name=name, state=state
    )


def _derive_pipe(event: Event) -> AlertIdentity | None:
    # Idealized contract used by manual publishers / future crons:
    # "Source | name | state". Real Gatus/Grafana are handled above.
    parts = [p.strip() for p in event.title.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    source = parts[0].lower()
    name = parts[1]
    state = EventState.UNKNOWN
    if len(parts) >= 3:
        token = parts[2].lower()
        if token.startswith("fir"):
            state = EventState.FIRING
        elif token.startswith("res"):
            state = EventState.RESOLVED
    return AlertIdentity(key=f"{source}:{name}", source=source, name=name, state=state)


def derive_identity(event: Event) -> AlertIdentity:
    """Derive the stable identity for ``event`` via ordered per-source rules."""
    title = event.title or ""
    if title.startswith(_GATUS_PREFIX):
        return _derive_gatus_native(event)
    marker = _GRAFANA_STATE.search(title)
    if marker:
        return _derive_grafana(event, marker)
    piped = _derive_pipe(event)
    if piped is not None:
        return piped
    return AlertIdentity(
        key=f"other:{normalized_title(title)}",
        source="other",
        name=title.strip() or "(untitled event)",
        state=EventState.UNKNOWN,
    )
