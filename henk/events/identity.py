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

# Per-rule identity scoping (sensor-routing-coverage, design D9). A rule opts in by carrying
# an `identity_scope: <labelname>` label; the value of the named label is appended to the key,
# so one alert name firing for several subjects yields several identities. Without this,
# HenkInstanceDown is ONE key for seven scrape targets and the second host to fail inside the
# cooldown window is silently swallowed.
#
# Opt-in, never automatic: all four pre-existing rules' metrics carry an `instance` label, so
# appending it whenever present would silently re-key every one of them.
_GRAFANA_SCOPE = re.compile(
    r"^[ \t]*-[ \t]*identity_scope[ \t]*=[ \t]*(\S+)[ \t]*$", re.MULTILINE
)
# Bound on the appended discriminator. Event payloads are untrusted data (design D4), and the
# key is persisted in cooldown state — an unbounded label value would grow that without limit.
_SCOPE_VALUE_MAX = 120


def _grafana_label(message: str, label: str) -> str | None:
    """Value of one label from Grafana's rendered ``Labels:`` block, or None.

    Anchored on the ``- <label> = `` line form on purpose: an unanchored search for
    ``name`` would match inside ``alertname = ...``, silently keying the container rule
    on its own alert name.
    """
    match = re.search(
        rf"^[ \t]*-[ \t]*{re.escape(label)}[ \t]*=[ \t]*(.*?)[ \t]*$",
        message,
        re.MULTILINE,
    )
    if match is None:
        return None
    return match.group(1) or None


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
    key = f"grafana:{name}"
    scope = _GRAFANA_SCOPE.search(event.message)
    if scope is not None:
        value = _grafana_label(event.message, scope.group(1))
        if value:
            # A rule may name a label the payload does not carry (misconfiguration, or a
            # grouped notification that dropped it). Degrading to the alertname-only key is
            # today's behaviour — safer than inventing a key or failing the intake.
            key = f"{key}/{value[:_SCOPE_VALUE_MAX]}"
    return AlertIdentity(key=key, source="grafana", name=name, state=state)


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
