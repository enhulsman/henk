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


# --- Per-rule identity scoping (sensor-routing-coverage, design D9) --------
#
# One alert NAME can fire concurrently for several distinct subjects: HenkInstanceDown
# covers seven scrape targets, HenkContainerRestarting every container. Keyed on alertname
# alone, the second subject to fail inside the 6h cooldown is silently swallowed — a real
# outage reported to nobody, with no error anywhere.
#
# A rule opts in by carrying `identity_scope: <labelname>`; the intake appends that label's
# value. Opt-in rather than automatic because all four pre-existing metrics carry `instance`,
# so scoping unconditionally would silently re-key every existing rule.
#
# Instance labels here use hostnames, not the real tailnet addresses (repo hygiene).

_SCOPED_BODY = """**Firing**

Value: A=0, B=0, C=1
Labels:
 - alertname = HenkInstanceDown
 - grafana_folder = henk
 - identity_scope = instance
 - instance = {instance}
 - job = {job}
 - route = henk-events
 - severity = critical
Annotations:
 - summary = Grafana | HenkInstanceDown | scrape target {instance} ({job}) is unreachable
"""


def _scoped(instance: str, job: str, state: str = "FIRING") -> Event:
    marker = "🚨 [FIRING:1]" if state == "FIRING" else "✅ [RESOLVED]"
    body = _SCOPED_BODY.format(instance=instance, job=job)
    if state != "FIRING":
        body = body.replace("**Firing**", "**Resolved**")
    return _event(f"{marker} HenkInstanceDown henk (henk-events)", body)


def test_identity_scope_distinguishes_two_down_targets():
    # THE headline case: two exporters down at once must not collapse into one identity,
    # or the second incident is eaten by the first one's cooldown.
    pi5 = derive_identity(_scoped("pi5:9100", "node-exporter-pi5"))
    vps = derive_identity(_scoped("vps:9100", "node-exporter-vps"))
    assert pi5.key != vps.key
    assert pi5.key == "grafana:HenkInstanceDown/pi5:9100"
    assert vps.key == "grafana:HenkInstanceDown/vps:9100"
    # the alert NAME is still the alert name — only the key is scoped
    assert pi5.name == vps.name == "HenkInstanceDown"
    assert pi5.source == vps.source == "grafana"


def test_identity_scope_resolve_pairs_with_its_own_fire():
    # The discriminator comes from labels, never from state, so an incident keeps one key
    # across fire -> resolve. Otherwise recurrence and cooldown track transitions, not incidents.
    firing = derive_identity(_scoped("pi5:9100", "node-exporter-pi5", "FIRING"))
    resolved = derive_identity(_scoped("pi5:9100", "node-exporter-pi5", "RESOLVED"))
    assert firing.state is EventState.FIRING
    assert resolved.state is EventState.RESOLVED
    assert firing.key == resolved.key


def test_identity_scope_does_not_cross_subjects_on_resolve():
    # A resolve for one target must not satisfy another target's incident.
    assert (
        derive_identity(_scoped("pi5:9100", "node-exporter-pi5", "RESOLVED")).key
        != derive_identity(_scoped("vps:9100", "node-exporter-vps", "FIRING")).key
    )


def test_identity_scope_can_name_any_label():
    # HenkContainerRestarting scopes on `name`, not `instance`.
    ev = _event(
        "🚨 [FIRING:1] HenkContainerRestarting henk (henk-events)",
        "**Firing**\nLabels:\n"
        " - alertname = HenkContainerRestarting\n"
        " - identity_scope = name\n"
        " - instance = pi5:8083\n"
        " - name = henk-henk-1\n"
        " - route = henk-events\n"
        " - severity = warning\n",
    )
    assert derive_identity(ev).key == "grafana:HenkContainerRestarting/henk-henk-1"


def test_rule_without_identity_scope_keys_exactly_as_before():
    # The four pre-existing rules must be untouched by this change. Their metrics DO carry
    # an `instance` label, so a naive "append instance when present" would have re-keyed
    # all of them — which is why scoping is opt-in.
    ev = _event(
        "🚨 [FIRING:1] HenkDiskPressure henk (henk-events)",
        "**Firing**\nLabels:\n"
        " - alertname = HenkDiskPressure\n"
        " - instance = pi5:9100\n"
        " - route = henk-events\n"
        " - severity = warning\n",
    )
    assert derive_identity(ev).key == "grafana:HenkDiskPressure"


def test_identity_scope_naming_an_absent_label_degrades_to_alertname():
    # Defensive: a rule declares a scope label that the payload does not carry. Better to
    # key on the alertname (today's behaviour) than to invent a key or crash intake.
    ev = _event(
        "🚨 [FIRING:1] HenkInstanceDown henk (henk-events)",
        "**Firing**\nLabels:\n"
        " - alertname = HenkInstanceDown\n"
        " - identity_scope = instance\n"
        " - route = henk-events\n",
    )
    ident = derive_identity(ev)
    assert ident.key == "grafana:HenkInstanceDown"
    assert ident.source == "grafana"


def test_scoped_identity_is_never_the_normalized_title_fallback():
    # A fallback identity embeds the rendered title, which carries changing values — so every
    # fire looks new, cooldown never suppresses, and nothing errors. This is the trap the
    # henk-events deploy-verify was written to catch; assert it explicitly for scoped rules.
    ident = derive_identity(_scoped("pi5:9100", "node-exporter-pi5"))
    assert ident.source == "grafana"
    assert not ident.key.startswith("other:")
    assert normalized_title("🚨 [FIRING:1] HenkInstanceDown henk (henk-events)") not in ident.key


def test_identity_scope_is_deterministic():
    ev = _scoped("pi5:9100", "node-exporter-pi5")
    assert derive_identity(ev).key == derive_identity(ev).key


def test_scope_label_lookup_is_anchored_not_substring():
    # `alertname` ENDS WITH `name`. An unanchored search for the label `name` matches inside
    # `- alertname = HenkContainerRestarting`, which would key every container incident on the
    # rule name — collapsing all containers into one identity, i.e. exactly the bug this
    # feature exists to fix, reintroduced by the fix itself. The label line must be anchored.
    ev = _event(
        "🚨 [FIRING:1] HenkContainerRestarting henk (henk-events)",
        "**Firing**\nLabels:\n"
        " - alertname = HenkContainerRestarting\n"
        " - identity_scope = name\n"
        " - name = gatus\n",
    )
    assert derive_identity(ev).key == "grafana:HenkContainerRestarting/gatus"


def test_scope_value_is_length_bounded():
    # Event payloads are untrusted data (design D4) and the key is persisted in cooldown
    # state, so an attacker-supplied label must not grow it without limit.
    ev = _event(
        "🚨 [FIRING:1] HenkInstanceDown henk (henk-events)",
        "**Firing**\nLabels:\n"
        " - alertname = HenkInstanceDown\n"
        " - identity_scope = instance\n"
        f" - instance = {'A' * 5000}\n",
    )
    key = derive_identity(ev).key
    assert len(key) < 200
    assert key.startswith("grafana:HenkInstanceDown/AAA")


def test_empty_scope_label_value_degrades_to_alertname():
    ev = _event(
        "🚨 [FIRING:1] HenkInstanceDown henk (henk-events)",
        "**Firing**\nLabels:\n"
        " - alertname = HenkInstanceDown\n"
        " - identity_scope = instance\n"
        " - instance = \n",
    )
    assert derive_identity(ev).key == "grafana:HenkInstanceDown"
