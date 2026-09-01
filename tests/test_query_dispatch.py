"""Dispatch, domain validation and the transport boundary (§3.4-3.7).

From `specs/homelab-tools`: "An unregistered query name is refused before any
request", "Validation happens in the tool, not only in the schema", "Injection
through a bound parameter is not possible", "The declared enum and the dispatch
table cannot diverge", "An out-of-domain node is rejected, not answered emptily",
"The same value is valid for a query whose domain includes it", "Every enumerated
domain is enforced", "In-domain but not currently derivable is its own outcome".

**Every refusal test asserts on the transport, not on the return value.** The
tool is handed a client whose transport fails the test if it is called at all, so
"no request was issued" is a property of the run rather than of the assertion
that follows it. The accepted path in `test_an_accepted_query_reaches_the_backend`
is what keeps that non-vacuous: if nothing ever reached a backend, the refusal
tests would pass with the validation deleted.
"""

from __future__ import annotations

import httpx
import pytest

from henk.tools.base import ToolClass
from henk.tools.homelab_query import HomelabQueryTool, build_parameters_schema
from henk.tools.query_registry import (
    QUERY_NAMES,
    QUERY_REGISTRY,
    QueryOutcome,
    QueryRefused,
    domain_for,
    expected_point_count,
    plan_query,
)

# Placeholder addresses only, per the in-repo fixture convention: no test may
# carry a real backend address.
PROMETHEUS = "http://10.0.0.1:9090"
GATUS = "http://10.0.0.2:8080"

#: A Gatus endpoint key as the deployed instance composes them
#: (`group_name`, lowercased, spaces to hyphens) — invented, not observed.
DISCOVERED_ENDPOINTS = ("core_web-front", "backups_nightly-dump")


class Recorder:
    """A transport handler that records every request it is asked to make."""

    def __init__(self, payload=None):
        self.requests: list[httpx.Request] = []
        self._payload = payload or {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self._payload)


def _refusing_transport(request: httpx.Request) -> httpx.Response:
    raise AssertionError(
        f"a refused invocation issued an HTTP request to {request.url}"
    )


def _tool(handler=None, *, discovered=None, max_points=60) -> HomelabQueryTool:
    return HomelabQueryTool(
        httpx.AsyncClient(
            transport=httpx.MockTransport(handler or _refusing_transport)
        ),
        gatus_url=GATUS,
        prometheus_url=PROMETHEUS,
        gatus_timeout=5.0,
        prometheus_timeout=5.0,
        max_points=max_points,
        discovered_endpoints=discovered,
    )


# --- 3.6 Refusals issue no request ----------------------------------------


async def test_an_unregistered_query_name_is_refused_before_any_request():
    result = await _tool().run(query_name="alerts_firing")
    assert result.ok is False
    assert "alerts_firing" in result.error
    # The refusal names what is available, so the model can correct itself.
    assert "node_resource_trend" in result.error


@pytest.mark.parametrize(
    "arguments",
    [
        {"query_name": "container_state", "node": "rp2"},
        {"query_name": "node_resource_trend", "node": "nas", "resource": "cpu", "window": "1h"},
        {"query_name": "node_resource_trend", "node": "rp5", "resource": "gpu", "window": "1h"},
        {"query_name": "node_resource_trend", "node": "rp5", "resource": "cpu", "window": "5m"},
        {"query_name": "node_resource_trend", "node": "rp5", "resource": "cpu", "window": "7d"},
        {"query_name": "dns_performance", "node": "adguard", "window": "1h"},
        {"query_name": "dns_performance", "node": "rp5", "window": "30d"},
        {"query_name": "endpoint_history", "endpoint": "core_web-front", "window": "6h"},
    ],
)
async def test_every_out_of_domain_value_is_refused_with_no_request(arguments):
    result = await _tool(discovered=DISCOVERED_ENDPOINTS).run(**arguments)
    assert result.ok is False
    bad = [v for k, v in arguments.items() if k != "query_name"]
    assert any(value in result.error for value in bad), result.error


@pytest.mark.parametrize(
    "value",
    [
        'rp5"} or up{job="node-exporter-vps',
        "rp5|vps",
        "../../health",
        "rp5}",
        "{job=~'.*'}",
        "",
    ],
)
async def test_injection_shaped_values_fail_domain_validation(value):
    # PromQL, selector and path syntax are not special-cased: they are simply not
    # members of a closed set, which is why the set is the boundary rather than a
    # sanitiser.
    result = await _tool().run(
        query_name="node_resource_trend", node=value, resource="cpu", window="1h"
    )
    assert result.ok is False


async def test_a_missing_parameter_is_refused_rather_than_defaulted():
    result = await _tool().run(query_name="node_resource_trend", node="rp5")
    assert result.ok is False
    assert "resource" in result.error and "window" in result.error


async def test_an_unknown_parameter_is_refused_rather_than_ignored():
    # A parameter the registry does not declare must not be silently dropped:
    # dropping it answers a different question than the one asked.
    result = await _tool().run(query_name="scrape_targets", node="rp5")
    assert result.ok is False
    assert "node" in result.error


async def test_a_parameter_from_another_query_is_refused():
    result = await _tool().run(query_name="container_state", node="rp5", resource="cpu")
    assert result.ok is False
    assert "resource" in result.error


# --- 3.4 Per-query domains ------------------------------------------------


async def test_container_state_rejects_rp2_with_an_error_not_an_empty_list():
    result = await _tool().run(query_name="container_state", node="rp2")
    assert result.ok is False
    assert "rp2" in result.error
    assert "container_state" in result.error
    # The distinction the message must carry: not measured here, rather than
    # measured and empty. rp2 runs no cadvisor.
    assert "rp5" in result.error and "vps" in result.error


async def test_node_resource_trend_accepts_rp2_and_queries_its_own_job():
    recorder = Recorder()
    result = await _tool(recorder).run(
        query_name="node_resource_trend", node="rp2", resource="load", window="1h"
    )
    assert recorder.requests, "an in-domain value must reach the backend"
    sent = str(recorder.requests[0].url)
    assert "node-exporter-pi2" in httpx.URL(sent).params["query"]
    assert result is not None


@pytest.mark.parametrize("resource", sorted(domain_for("node_resource_trend", "resource")))
@pytest.mark.parametrize("window", sorted(domain_for("node_resource_trend", "window")))
async def test_every_enumerated_resource_and_window_is_accepted(resource, window):
    recorder = Recorder()
    await _tool(recorder).run(
        query_name="node_resource_trend", node="rp5", resource=resource, window=window
    )
    # rp5 reports every one of the seven, so nothing here is short-circuited as
    # underivable: each combination must actually reach Prometheus.
    assert recorder.requests, f"{resource}/{window} must be queryable on rp5"


# --- 3.5 Three distinguishable outcomes -----------------------------------


async def test_the_three_outcomes_are_distinguishable():
    recorder = Recorder()
    answered = await _tool(recorder).run(
        query_name="node_resource_trend", node="rp5", resource="temperature", window="1h"
    )
    refused = await _tool().run(query_name="container_state", node="rp2")
    # In domain (the spec declares `node` uniform for every resource) but not
    # derivable: neither temperature metric exists on the vps (record 1.4).
    underivable = await _tool().run(
        query_name="node_resource_trend", node="vps", resource="temperature", window="1h"
    )

    assert recorder.requests, "the available case must actually query"
    assert refused.ok is False
    assert underivable.ok is True, (
        "an in-domain value whose data cannot be obtained is a RESULT stating "
        "that condition, not a refusal and not an empty measurement"
    )
    message = underivable.content
    assert "vps" in message and "temperature" in message
    assert "outside" not in message, "this is not an out-of-domain rejection"
    # And it does not read as a measurement that came back empty.
    assert "no data" in message.lower() or "not" in message.lower()
    assert message != (refused.error or "")


async def test_an_underivable_combination_issues_no_pointless_request():
    # There is no series to fetch, so fetching one would produce an empty result
    # that reads exactly like "measured, nothing there".
    result = await _tool().run(
        query_name="node_resource_trend", node="vps", resource="temperature", window="24h"
    )
    assert result.ok is True


async def test_container_state_on_rp5_still_answers_but_carries_its_caveat():
    # `container_health_state` has no series on cadvisor-pi5 (record 1.4). That
    # is a missing FIELD, not a missing query: last-seen, OOM events and creation
    # time are all available, so the query runs and the result says which column
    # is unavailable. An omitted health column reads as "nothing is unhealthy".
    plan = plan_query("container_state", {"node": "rp5"})
    assert plan.outcome is QueryOutcome.ANSWERED
    assert any("health_state" in caveat for caveat in plan.caveats)
    assert not any("health_state" in caveat for caveat in plan_query(
        "container_state", {"node": "vps"}
    ).caveats)


def test_out_of_domain_raises_a_refusal_carrying_its_own_outcome():
    with pytest.raises(QueryRefused) as exc:
        plan_query("container_state", {"node": "rp2"})
    assert exc.value.outcome is QueryOutcome.OUT_OF_DOMAIN
    assert plan_query("container_state", {"node": "rp5"}).outcome is QueryOutcome.ANSWERED
    assert plan_query(
        "node_resource_trend", {"node": "vps", "resource": "temperature", "window": "1h"}
    ).outcome is QueryOutcome.NOT_DERIVABLE


# --- 3.7 The schema is not the boundary -----------------------------------


async def test_validation_holds_when_the_schema_layer_is_bypassed():
    # Model arguments are splatted straight into `_run`; whether the SDK's MCP
    # layer enforces `input_schema` carries a standing "verify at deploy" note in
    # this codebase. So the in-process check is the normative boundary, and this
    # test exercises it by calling the tool directly with values the schema
    # would have rejected.
    schema = build_parameters_schema()
    assert "nas" not in schema["properties"]["node"]["enum"]
    result = await _tool()._run(query_name="container_state", node="nas")
    assert result.ok is False
    assert "nas" in result.error


async def test_a_wholly_unknown_keyword_argument_is_refused_not_raised():
    # The SDK splats whatever the model produced. An unexpected keyword must be a
    # tool error, not a TypeError that surfaces as a crashed turn.
    result = await _tool().run(query_name="scrape_targets", promql="up{}")
    assert result.ok is False
    assert "promql" in result.error


def test_the_advertised_domain_and_the_enforced_domain_are_the_same_object():
    # Not "equal": the same tuple. Two equal copies can be edited apart, which is
    # exactly how a domain gets advertised that is not enforced.
    for name, entry in QUERY_REGISTRY.items():
        for parameter in entry.parameters:
            assert domain_for(name, parameter.name) is parameter.domain


def test_the_schema_advertises_no_value_that_no_entry_enforces():
    schema = build_parameters_schema()
    assert schema["properties"]["query_name"]["enum"] == list(QUERY_NAMES)
    assert schema["additionalProperties"] is False
    for parameter, spec in schema["properties"].items():
        if parameter == "query_name" or "enum" not in spec:
            continue
        enforced = {
            value
            for entry in QUERY_REGISTRY.values()
            for p in entry.parameters
            if p.name == parameter and p.domain
            for value in p.domain
        }
        assert set(spec["enum"]) == enforced, parameter


def test_the_schema_states_the_per_query_domains_it_cannot_express():
    # A JSON schema cannot say "rp2 is valid here and not there", so the
    # description must — derived from the registry, not written by hand.
    node = build_parameters_schema()["properties"]["node"]["description"]
    assert "container_state" in node and "rp5, vps" in node
    assert "node_resource_trend" in node and "rp5, vps, rp2" in node


def test_the_schema_exposes_no_free_text_query_field():
    schema = build_parameters_schema()
    for forbidden in ("expr", "promql", "metric", "selector", "path", "url"):
        assert not any(forbidden in name for name in schema["properties"]), forbidden
    # The only query-ish field is the closed enum itself — there is no bare
    # `query` beside it, and it carries an enum rather than free text.
    assert [n for n in schema["properties"] if "query" in n] == ["query_name"]
    assert schema["properties"]["query_name"]["enum"]
    assert schema["required"] == ["query_name"]


def test_the_discovered_parameter_advertises_no_enum_and_says_why():
    endpoint = build_parameters_schema()["properties"]["endpoint"]
    assert "enum" not in endpoint, (
        "the Gatus key set is discovered at first use; a hardcoded enum would rot "
        "against a config the owner edits by hand"
    )
    assert "discover" in endpoint["description"].lower()


# --- endpoint_history: a discovered domain that still fails closed ---------


async def test_endpoint_history_fails_closed_when_discovery_has_not_run():
    result = await _tool(discovered=None).run(
        query_name="endpoint_history", endpoint="core_web-front", window="24h"
    )
    assert result.ok is False
    assert "endpoint" in result.error


async def test_endpoint_history_refuses_an_undiscovered_key_with_no_request():
    result = await _tool(discovered=DISCOVERED_ENDPOINTS).run(
        query_name="endpoint_history", endpoint="core_absent", window="24h"
    )
    assert result.ok is False
    assert "core_absent" in result.error


async def test_endpoint_history_accepts_a_discovered_key():
    recorder = Recorder(payload={"name": "x", "key": "core_web-front", "results": []})
    await _tool(recorder, discovered=DISCOVERED_ENDPOINTS).run(
        query_name="endpoint_history", endpoint="core_web-front", window="24h"
    )
    paths = [request.url.path for request in recorder.requests]
    assert any("core_web-front" in path for path in paths)
    assert any(path.endswith("/uptimes/24h") for path in paths)


def test_a_discovered_key_is_percent_encoded_into_the_route():
    # Discovered keys derive from owner free text, so discovery is a traversal
    # source in its own right — the spec requires encoding or rejection even
    # though all 19 live keys are currently safe.
    plan = plan_query(
        "endpoint_history",
        {"endpoint": "core_a b/../c", "window": "1h"},
        discovered={"endpoint": ("core_a b/../c",)},
    )
    for route in plan.routes.values():
        assert "/../" not in route
        assert " " not in route
        assert "%2F" in route or "%20" in route


# --- Bounded range requests, read-only class ------------------------------


@pytest.mark.parametrize("window", sorted(domain_for("node_resource_trend", "window")))
def test_the_point_count_never_exceeds_the_configured_maximum(window):
    for max_points in (2, 10, 60, 500):
        assert expected_point_count(window, max_points) <= max_points


async def test_a_range_query_asks_for_a_bounded_number_of_points():
    recorder = Recorder()
    await _tool(recorder, max_points=10).run(
        query_name="node_resource_trend", node="rp5", resource="memory", window="24h"
    )
    params = recorder.requests[0].url.params
    assert recorder.requests[0].url.path.endswith("/query_range")
    span = float(params["end"]) - float(params["start"])
    assert span / float(params["step"]) + 1 <= 10


def test_the_tool_is_read_only_and_carries_no_authorization_tier():
    tool = _tool()
    assert tool.tool_class is ToolClass.READ_ONLY
    assert tool.authorization is None
    assert tool.name == "homelab_query"


async def test_an_accepted_query_reaches_the_backend_and_awaits_its_renderer():
    # §4 replaces the pending branch with the real summary. Until then the tool
    # says so explicitly rather than returning something that looks like data —
    # and, critically for the refusal tests above, an accepted query DOES issue a
    # request, so "no request was issued" is a real signal.
    recorder = Recorder()
    result = await _tool(recorder).run(query_name="scrape_targets")
    assert recorder.requests
    assert result.ok is False
    assert "scrape_targets" in result.error
