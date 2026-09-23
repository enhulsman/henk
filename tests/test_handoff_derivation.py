"""Message ids, rule keys and nodes for the handoff archive (task 6.3).

From design D8 ("Ids") and D9 (the rule key and the node derivation), and the
triage-handoff spec: an incident's nodes are derived from its title and message by
whole-word match against the closed node set and the known exporter job names
only, and are never stored as addresses.

Placeholders only (standing rule 1): `host-a.example`, `example-a.service` and
RFC 5737 addresses (`192.0.2.x`).
"""

from __future__ import annotations

import re

import pytest

from henk.events.identity import derive_identity
from henk.events.incident_context import (
    EMPTY_INCIDENT_CONTEXT,
    NODES,
    IncidentContext,
    IncidentContextProvider,
    derive_nodes,
    derive_rule_key,
)
from henk.events.types import AlertIdentity, Event, EventState
from henk.agent.turns import EventTurn, EventTurnItem
from henk.store.handoffs import format_handoff_result, parse_handoff_message_id
from henk.tools.query_projection import NODE_FOR_JOB

_ADDRESS = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}|://|:\d{2,5}\b")


def _event(title: str, message: str = "", event_id: str = "ev-1") -> Event:
    return Event(id=event_id, title=title, message=message, arrival_time=0.0)


def _grafana(alertname: str, labels: dict[str, str], *, state: str = "FIRING") -> Event:
    lines = "\n".join(f" - {k} = {v}" for k, v in labels.items())
    message = (
        "Value: A=98.39\n"
        f"Labels:\n - alertname = {alertname}\n{lines}\n"
        "Annotations:\n - summary = swap is filling\n"
        "Source: https://grafana.host-a.example/alerting/grafana/abc/view\n"
        "Silence: https://grafana.host-a.example/alerting/silence/new?matcher=x\n"
    )
    return _event(f"[{state}:1] {alertname} henk (192.0.2.10:9100)", message)


def _item(event: Event) -> EventTurnItem:
    return EventTurnItem(event=event, identity=derive_identity(event))


# --- Message ids: both forms ----------------------------------------------


def test_the_result_string_and_the_parser_agree():
    assert format_handoff_result("hf-1") == "handoff published (id: hf-1)"
    assert parse_handoff_message_id(format_handoff_result("hf-1")) == "hf-1"


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("handoff published (id: Ab3xYz9)", "Ab3xYz9"),
        ("Ab3xYz9", "Ab3xYz9"),
        ("  Ab3xYz9  ", "Ab3xYz9"),
        ("  handoff published (id: Ab3xYz9)  ", "Ab3xYz9"),
        ("handoff published (id:  Ab3xYz9 )", "Ab3xYz9"),
    ],
)
def test_message_id_parsing_accepts_both_forms(ref, expected):
    assert parse_handoff_message_id(ref) == expected


@pytest.mark.parametrize(
    "ref", ["", "   ", None, "handoff published (id: )", "handoff published (id:    )"]
)
def test_an_empty_id_parses_to_none(ref):
    assert parse_handoff_message_id(ref) is None


def test_an_empty_publish_result_formats_and_parses_back_to_none():
    # The tool's result when ntfy returned no id (publish_handoff.py) is exactly
    # this string; it must never round-trip as a real id.
    assert parse_handoff_message_id(format_handoff_result("")) is None


# --- Rule keys ------------------------------------------------------------


def test_the_rule_key_drops_the_identity_scope_suffix():
    scoped = _grafana(
        "HenkInstanceDown",
        {"identity_scope": "job", "job": "node-exporter-pi5"},
    )
    identity = derive_identity(scoped)
    assert identity.key == "grafana:HenkInstanceDown/node-exporter-pi5"
    assert derive_rule_key(identity) == "grafana:HenkInstanceDown"


def test_one_rules_subjects_share_a_rule_key():
    a = derive_identity(
        _grafana("HenkContainerDown", {"identity_scope": "name", "name": "example-a"})
    )
    b = derive_identity(
        _grafana("HenkContainerDown", {"identity_scope": "name", "name": "example-b"})
    )
    assert a.key != b.key
    assert derive_rule_key(a) == derive_rule_key(b) == "grafana:HenkContainerDown"


def test_an_unscoped_rule_key_is_its_identity_key():
    # The 1.5 probe: HenkSwapPressure carries no identity_scope, so tiers 1 and 2
    # coincide for it.
    identity = derive_identity(_grafana("HenkSwapPressure", {"job": "node-exporter-vps"}))
    assert identity.key == "grafana:HenkSwapPressure"
    assert derive_rule_key(identity) == identity.key


def test_a_scoped_address_never_reaches_the_rule_key():
    # A rule scoped on an address-bearing label puts the address in the identity
    # key; the rule key is the part before the scope, and carries none.
    identity = derive_identity(
        _grafana(
            "HenkInstanceDown",
            {"identity_scope": "instance", "instance": "192.0.2.7:9100"},
        )
    )
    assert "192.0.2.7" in identity.key
    rule = derive_rule_key(identity)
    assert rule == "grafana:HenkInstanceDown"
    assert not _ADDRESS.search(rule)


@pytest.mark.parametrize(
    "event",
    [
        _event("Gatus: core/example-a", "An alert for core/example-a has been triggered"),
        _event("Backup | nightly-example | firing"),
        _event("Something Odd   Happened"),
    ],
)
def test_non_grafana_rule_keys_are_the_identity_key(event):
    # No other source has a scope suffix, and the `other` fallback's key is the
    # NORMALIZED title while its name is the raw one: f"{source}:{name}" would
    # give two events of one identity two different rule keys.
    identity = derive_identity(event)
    assert derive_rule_key(identity) == identity.key


def test_the_rule_key_is_a_prefix_of_the_identity_key():
    for event in (
        _grafana("HenkSwapPressure", {"job": "node-exporter-vps"}),
        _grafana("HenkContainerDown", {"identity_scope": "name", "name": "x/y"}),
        _event("Gatus: core/example-a"),
        _event("Other | thing"),
    ):
        identity = derive_identity(event)
        rule = derive_rule_key(identity)
        assert identity.key == rule or identity.key.startswith(rule + "/")


# --- Nodes: whole-word, closed set, never an address ------------------------


def test_the_closed_node_set():
    assert NODES == ("rp5", "vps", "rp2")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("swap pressure on vps", {"vps"}),
        ("rp5 is hot", {"rp5"}),
        ("RP2 unreachable", {"rp2"}),
        ("rp5 and vps and rp2", {"rp5", "vps", "rp2"}),
        ("vps/ntfy failed", {"vps"}),
        ("(rp5)", {"rp5"}),
        ("rp5,vps", {"rp5", "vps"}),
    ],
)
def test_a_node_named_as_a_whole_word_is_derived(text, expected):
    assert derive_nodes(text, "") == frozenset(expected)


@pytest.mark.parametrize(
    "text",
    [
        "myvps went down",
        "vpsx is a different host",
        "rp50 is not rp5",
        "the rp5_backup job",
        "arp2 cache",
        "vps2 is not the vps",
        "host-a.example and example-a.service",
    ],
)
def test_a_node_token_inside_a_longer_word_is_not_derived(text):
    derived = derive_nodes(text, "")
    # "rp50 is not rp5" names rp5 as a whole word at its end, and "vps2 is not the
    # vps" names vps; neither may pick up anything from the longer tokens.
    extra = {"rp50 is not rp5": {"rp5"}, "vps2 is not the vps": {"vps"}}
    assert derived == frozenset(extra.get(text, set()))


@pytest.mark.parametrize(("job", "node"), sorted(NODE_FOR_JOB.items()))
def test_every_known_exporter_job_maps_to_its_node(job, node):
    assert derive_nodes("", f" - job = {job}") == frozenset({node})


def test_a_job_name_is_matched_as_a_whole_word_too():
    assert derive_nodes("", " - job = node-exporter-pi5x") == frozenset()
    assert derive_nodes("", " - job = xcadvisor-pi5") == frozenset()


def test_nodes_come_from_the_title_and_the_message():
    assert derive_nodes("[FIRING:1] X on rp2", " - job = cadvisor-vps") == frozenset(
        {"rp2", "vps"}
    )


def test_an_address_never_becomes_a_node():
    title = "[FIRING:1] HenkInstanceDown henk (192.0.2.10:9100, 192.0.2.11:8080)"
    message = " - instance = 192.0.2.10:9100\n - server = http://192.0.2.12:3000"
    assert derive_nodes(title, message) == frozenset()


def test_a_url_contributes_no_node():
    # 1.5 probe: the Source:/Silence: URLs point at the Grafana host, not at the
    # alert's subject. A Grafana host whose name holds a node token would tag
    # every alert with that node, making "same node" relate everything.
    message = (
        " - job = node-exporter-pi5\n"
        "Source: https://grafana.vps.example/alerting/grafana/abc/view\n"
        "Silence: http://vps:3000/alerting/silence/new?matcher=rp2\n"
    )
    assert derive_nodes("[FIRING:1] HenkSwapPressure henk", message) == frozenset(
        {"rp5"}
    )


def test_derived_nodes_are_always_members_of_the_closed_set():
    samples = [
        _grafana("HenkSwapPressure", {"job": "node-exporter-vps",
                                      "instance": "192.0.2.10:9100"}),
        _grafana("HenkContainerDown", {"job": "cadvisor-pi5", "name": "example-a"}),
        _event("Gatus: vps/ntfy", "An alert for vps/ntfy has been triggered"),
        _event("rp5 | disk | firing", "disk 91% on rp5 (192.0.2.4)"),
        _event("[FIRING:2] rp2 vps node-exporter-pi2", "http://192.0.2.9:9100/metrics"),
    ]
    for event in samples:
        nodes = derive_nodes(event.title, event.message)
        assert nodes <= set(NODES)
        for node in nodes:
            assert not _ADDRESS.search(node)


# --- IncidentContext and its provider --------------------------------------


def test_the_context_is_built_from_the_turns_items():
    swap = _item(_grafana("HenkSwapPressure", {"job": "node-exporter-vps"}))
    down_a = _item(_grafana("HenkContainerDown", {"identity_scope": "name",
                                                  "name": "example-a",
                                                  "job": "cadvisor-pi5"}))
    down_b = _item(_grafana("HenkContainerDown", {"identity_scope": "name",
                                                  "name": "example-b",
                                                  "job": "cadvisor-pi5"}))
    context = IncidentContext.from_turn(EventTurn(items=(swap, down_a, down_b)))
    assert context.identity_keys == (
        "grafana:HenkSwapPressure",
        "grafana:HenkContainerDown/example-a",
        "grafana:HenkContainerDown/example-b",
    )
    # De-duplicated, first-seen order.
    assert context.rule_keys == ("grafana:HenkSwapPressure", "grafana:HenkContainerDown")
    # Closed-set order, never an address.
    assert context.nodes == ("rp5", "vps")
    assert not context.empty


def test_the_context_carries_no_address():
    item = _item(
        _grafana("HenkInstanceDown", {"identity_scope": "job", "job": "node-exporter-pi2",
                                      "instance": "192.0.2.7:9100"})
    )
    context = IncidentContext.from_items([item])
    assert not any(_ADDRESS.search(v) for v in context.rule_keys + context.nodes)


def test_an_empty_context_is_empty():
    assert EMPTY_INCIDENT_CONTEXT.empty
    assert IncidentContext.from_items([]).empty
    assert IncidentContext().identity_keys == ()


def test_the_provider_starts_empty_publishes_and_clears():
    provider = IncidentContextProvider()
    assert provider.current().empty
    context = IncidentContext(
        identity_keys=("gatus:core/example-a",),
        rule_keys=("gatus:core/example-a",),
        nodes=(),
    )
    provider.publish(context)
    assert provider.current() is context
    provider.clear()
    assert provider.current().empty


def test_the_context_is_immutable():
    context = IncidentContext(identity_keys=("a:b",), rule_keys=("a:b",), nodes=())
    with pytest.raises(Exception):
        context.identity_keys = ("x:y",)  # type: ignore[misc]


def test_a_non_grafana_identity_object_is_accepted():
    identity = AlertIdentity(key="gatus:core/example-a", source="gatus",
                             name="core/example-a", state=EventState.FIRING)
    assert derive_rule_key(identity) == "gatus:core/example-a"
