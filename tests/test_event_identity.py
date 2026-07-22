"""Stable-alert-identity tests (task 2.1), from specs/event-intake.

Fixtures are the REAL captured ntfy payloads (tests/fixtures/ntfy_events/) plus
the two resolved variants the README says to synthesize (Gatus/Grafana resolved
were not captured live). The identity key is what cooldown/dedup/recurrence key
on, so the invariants under test are: same alert → same key across fire/resolve,
and every nonconforming event still gets a deterministic fallback key.
"""

from __future__ import annotations

import json
from pathlib import Path

from henk.events.identity import derive_identity, normalized_title
from henk.events.types import Event, EventState

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "ntfy_events"
    / "henk-events-live.jsonl"
)


def _events_from_fixture() -> list[Event]:
    events = []
    for line in FIXTURE.read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        events.append(
            Event(
                id=obj["id"],
                title=obj.get("title", ""),
                message=obj.get("message", ""),
                arrival_time=0.0,
                raw=obj,
            )
        )
    return events


def _event(title: str, message: str = "") -> Event:
    return Event(id="x", title=title, message=message, arrival_time=0.0)


# --- Real captured payloads -----------------------------------------------


def test_pipe_delimited_manual_publish():
    # Fixture line 1: hand-published, conforms to the idealized pipe title.
    events = _events_from_fixture()
    ident = derive_identity(events[0])
    assert ident.source == "gatus"
    assert ident.name == "smoke-test"
    assert ident.state is EventState.FIRING
    assert ident.key == "gatus:smoke-test"


def test_gatus_native_title_keys_on_group_endpoint():
    # Fixture line 2: real Gatus — title `Gatus: {group}/{endpoint}`, state in body.
    events = _events_from_fixture()
    ident = derive_identity(events[1])
    assert ident.source == "gatus"
    assert ident.name == "test/henk-smoke"
    assert ident.state is EventState.FIRING  # "has been triggered ..."
    assert ident.key == "gatus:test/henk-smoke"


def test_grafana_firing_keys_on_alertname():
    # Fixture line 3: real Grafana via ntfy ?template=grafana.
    events = _events_from_fixture()
    ident = derive_identity(events[2])
    assert ident.source == "grafana"
    assert ident.name == "HenkProvisionSmoke"
    assert ident.state is EventState.FIRING
    assert ident.key == "grafana:HenkProvisionSmoke"


# --- Synthesized resolved variants (README: not captured live) ------------


def test_gatus_resolved_same_key_as_firing():
    firing = _event(
        "Gatus: test/henk-smoke",
        "An alert has been triggered due to having failed 2 time(s) in a row",
    )
    resolved = _event(
        "Gatus: test/henk-smoke",
        "An alert has been resolved after passing successfully 3 time(s) in a row",
    )
    fk, rk = derive_identity(firing), derive_identity(resolved)
    assert rk.state is EventState.RESOLVED
    assert fk.state is EventState.FIRING
    assert fk.key == rk.key  # incident identity is state-independent


def test_grafana_resolved_same_key_as_firing():
    firing = _event(
        "🚨 [FIRING:1] HenkHealthEtl henk (henk-events)",
        "**Firing**\nLabels:\n - alertname = HenkHealthEtl\n",
    )
    resolved = _event(
        "✅ [RESOLVED] HenkHealthEtl henk (henk-events)",
        "**Resolved**\nLabels:\n - alertname = HenkHealthEtl\n",
    )
    fk, rk = derive_identity(firing), derive_identity(resolved)
    assert fk.state is EventState.FIRING
    assert rk.state is EventState.RESOLVED
    assert fk.key == rk.key == "grafana:HenkHealthEtl"


# --- Determinism and fallback ---------------------------------------------


def test_same_event_derives_same_key():
    e = _event("Gatus: prod/api", "An alert has been triggered")
    assert derive_identity(e).key == derive_identity(e).key


def test_nonconforming_event_gets_deterministic_fallback():
    weird = _event("total gibberish 12:34 !!", "no contract here")
    a = derive_identity(weird)
    b = derive_identity(weird)
    assert a.key == b.key  # deterministic
    assert a.source == "other"
    assert a.state is EventState.UNKNOWN
    assert a.key == f"other:{normalized_title('total gibberish 12:34 !!')}"


def test_empty_title_still_keyed():
    e = _event("", "")
    ident = derive_identity(e)
    assert ident.key  # never empty — a keyless event would break cooldown/dedup
