"""The six named queries and their renderers (§4.1-4.10).

From `specs/homelab-tools`: "Templates select by job label and results carry no
addresses", "Range queries return bounded summaries", "Threshold comparisons are
per-resource and traceable to a measurement", "scrape_targets proves it ran and
says why a target is down", "container_state declares both what it cannot see and
what it omits", "freshness_check reports raw timestamps", "endpoint_history's
domain is discovered at first use and fails closed", "The event that fired maps to
a queryable endpoint argument", "DNS node identification is derived, never
configured or hardcoded".

**Every fixture in this module carries address-shaped data by construction** —
that is the point: a renderer is only proved to project if the payload it is
handed contains something to drop. All of it is placeholder (`10.0.0.x`,
`example.invalid`), never a real backend address, and the assertions are on the
placeholder VALUES rather than on label names, because a label name can
legitimately appear in prose while its value never may.

Nothing here reads the wall clock. Prometheus stamps every sample with its own
evaluation time and the renderers derive ages from that, which is also what makes
the frozen-writer test possible: the same raw timestamp against two evaluation
times must yield two different, growing ages.
"""

from __future__ import annotations

import re

import httpx
import pytest

from henk.events.identity import derive_identity
from henk.events.types import Event
from henk.tools.homelab_query import HomelabQueryTool
from henk.tools.query_registry import (
    QUERY_REGISTRY,
    QueryRefused,
    compose_gatus_key,
    named_container_expression,
    plan_query,
    resolve_endpoint_key,
    scrub_addresses,
)
from henk.tools.query_renderers import render_named_container_reading

# --- Placeholder fixture data ---------------------------------------------

PROMETHEUS = "http://10.0.0.1:9090"
GATUS = "http://10.0.0.2:8080"

#: Every address a fixture may carry. Assertions sweep this list over rendered
#: output, so a renderer that leaks any one of them fails somewhere.
PLACEHOLDER_ADDRESSES = (
    "10.0.0.1",
    "10.0.0.2",
    "10.0.0.5",
    "10.0.0.6",
    "10.0.0.7",
    "10.0.0.8",
)

NODE_INSTANCE = {
    "rp5": "10.0.0.5:9100",
    "vps": "10.0.0.6:9100",
    "rp2": "10.0.0.7:9100",
}
ADGUARD_SERVER = {
    "rp5": "http://10.0.0.5:3000",
    "vps": "http://10.0.0.6:3000",
    "rp2": "http://10.0.0.7:3000",
}
ADGUARD_INSTANCE = "10.0.0.5:9618"

NOW = 1_756_000_000.0
STEP = 600.0


def vector(*series: dict) -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": list(series)}}


def matrix(*series: dict) -> dict:
    return {"status": "success", "data": {"resultType": "matrix", "result": list(series)}}


def sample(metric: dict, value: float, *, at: float = NOW) -> dict:
    return {"metric": dict(metric), "value": [at, f"{value}"]}


def series(metric: dict, values: list[float], *, end: float = NOW) -> dict:
    start = end - STEP * (len(values) - 1)
    return {
        "metric": dict(metric),
        "values": [[start + STEP * i, f"{v}"] for i, v in enumerate(values)],
    }


def node_metric(node: str, job: str, **extra: str) -> dict:
    return {"job": job, "instance": NODE_INSTANCE[node], **extra}


def assert_no_addresses(text: str) -> None:
    for address in PLACEHOLDER_ADDRESSES:
        assert address not in text, f"{address} leaked into the rendered result"
    assert "example.invalid" not in text
    assert ".ts.net" not in text


def render(query_name: str, arguments: dict | None = None, *, discovered=None, **payloads):
    plan = plan_query(query_name, arguments or {}, discovered=discovered)
    return QUERY_REGISTRY[query_name].renderer(plan, payloads)


# --- Transport doubles -----------------------------------------------------


class Router:
    """A MockTransport handler routing by path, recording every request."""

    def __init__(self, routes: dict[str, object], *, default: object | None = None):
        self.routes = routes
        self.default = default
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for fragment, payload in self.routes.items():
            if fragment in request.url.path:
                if isinstance(payload, int):
                    return httpx.Response(payload, json={"error": "boom"})
                if isinstance(payload, Exception):
                    raise payload
                return httpx.Response(200, json=payload)
        if self.default is None:
            raise AssertionError(f"unrouted request to {request.url}")
        return httpx.Response(200, json=self.default)

    @property
    def paths(self) -> list[str]:
        return [str(request.url) for request in self.requests]


def tool(handler=None, *, discovered=None, max_points=60, ttl=300.0, clock=None) -> HomelabQueryTool:
    return HomelabQueryTool(
        httpx.AsyncClient(transport=httpx.MockTransport(handler or _unrouted)),
        gatus_url=GATUS,
        prometheus_url=PROMETHEUS,
        gatus_timeout=5.0,
        prometheus_timeout=5.0,
        max_points=max_points,
        discovered_endpoints=discovered,
        endpoint_discovery_ttl=ttl,
        clock=clock or (lambda: NOW),
    )


def _unrouted(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP request to {request.url}")


# --- 4.1 Projection: no address reaches a rendered result ------------------


def test_a_trend_result_names_the_enum_value_and_carries_no_address():
    payload = matrix(series(node_metric("rp5", "node-exporter-pi5"), [40.0, 47.53, 52.25]))
    out = render(
        "node_resource_trend",
        {"node": "rp5", "resource": "memory", "window": "1h"},
        memory=payload,
    )
    assert_no_addresses(out)
    assert "rp5" in out
    assert "node-exporter-pi5" in out, "the job label is the safe identifier"


def test_scrape_targets_drops_every_address_bearing_field():
    out = render(
        "scrape_targets",
        {},
        up=vector(
            sample(node_metric("rp5", "node-exporter-pi5"), 1),
            sample(node_metric("rp2", "node-exporter-pi2"), 0),
        ),
        up_over_window=vector(
            sample(node_metric("rp5", "node-exporter-pi5"), 1),
            sample(node_metric("rp2", "node-exporter-pi2"), 0),
        ),
        targets={
            "status": "success",
            "data": {
                "activeTargets": [
                    {
                        "labels": {"job": "node-exporter-pi5", "instance": NODE_INSTANCE["rp5"]},
                        "discoveredLabels": {"__address__": NODE_INSTANCE["rp5"]},
                        "scrapeUrl": f"http://{NODE_INSTANCE['rp5']}/metrics",
                        "globalUrl": f"http://{NODE_INSTANCE['rp5']}/metrics",
                        "health": "up",
                        "lastError": "",
                    },
                    {
                        "labels": {"job": "node-exporter-pi2", "instance": NODE_INSTANCE["rp2"]},
                        "discoveredLabels": {"__address__": NODE_INSTANCE["rp2"]},
                        "scrapeUrl": f"http://{NODE_INSTANCE['rp2']}/metrics",
                        "globalUrl": f"http://{NODE_INSTANCE['rp2']}/metrics",
                        "health": "down",
                        # The live shape: the cause names the scrape URL, so the
                        # field the spec requires surfacing is itself an address
                        # carrier and must be scrubbed rather than dropped.
                        "lastError": (
                            f"Get \"http://{NODE_INSTANCE['rp2']}/metrics\": dial tcp "
                            f"{NODE_INSTANCE['rp2']}: connect: connection refused"
                        ),
                    },
                ]
            },
        },
    )
    assert_no_addresses(out)
    assert "rp2" in out and "node-exporter-pi2" in out
    assert "connection refused" in out, "the cause survives; only the address is scrubbed"


def test_an_address_bearing_label_is_dropped_by_NAME_not_only_by_value_shape():
    # The value-shape filter would catch `10.0.0.7:9100`. It does not catch a
    # host that is not written as an address — and `instance` on a MagicDNS
    # tailnet is exactly that. So the name denylist has to be load-bearing on its
    # own, and this fixture is the case that proves it is.
    out = render(
        "scrape_targets",
        {},
        up=vector(sample({"job": "cadvisor-pi5", "instance": "pi5-host-name:8080"}, 1)),
        up_over_window=vector(sample({"job": "cadvisor-pi5"}, 1)),
        targets=_targets_payload([("cadvisor-pi5", "up", "")]),
    )
    assert "pi5-host-name" not in out
    assert "rp5" in out and "cadvisor-pi5" in out


def test_the_address_scrubber_redacts_urls_hostnames_and_bare_addresses():
    scrubbed = scrub_addresses(
        'Get "http://10.0.0.7:9100/metrics": dial tcp 10.0.0.7:9100 on rp2.example.ts.net:443'
    )
    assert_no_addresses(scrubbed)
    assert "10.0.0.7" not in scrubbed
    assert "dial tcp" in scrubbed and "redacted" in scrubbed


def test_the_scrubber_leaves_address_free_text_untouched():
    text = "context deadline exceeded (Client.Timeout exceeded while awaiting headers)"
    assert scrub_addresses(text) == text


# --- 4.2 Series multiplicity is reported, never collapsed ------------------


def test_a_job_returning_two_series_is_reported_as_such():
    payload = matrix(
        series(node_metric("rp5", "node-exporter-pi5"), [40.0, 41.0]),
        series({"job": "node-exporter-pi5", "instance": "10.0.0.8:9100"}, [90.0, 91.0]),
    )
    out = render(
        "node_resource_trend",
        {"node": "rp5", "resource": "memory", "window": "1h"},
        memory=payload,
    )
    assert_no_addresses(out)
    assert "2 series" in out
    assert "Summary:" not in out, (
        "one of two series must not be presented as the target's figure"
    )
    assert "41.00" not in out and "91.00" not in out


def test_dns_reports_two_adguard_series_mapping_to_one_node():
    out = render(
        "dns_performance",
        {"node": "rp5", "window": "24h"},
        series=matrix(
            series(
                {"job": "adguard-exporter", "instance": ADGUARD_INSTANCE, "server": ADGUARD_SERVER["rp5"]},
                [0.0024, 0.0025],
            ),
            series(
                {
                    "job": "adguard-exporter",
                    "instance": ADGUARD_INSTANCE,
                    "server": "http://10.0.0.5:3001",
                },
                [0.0900, 0.0910],
            ),
        ),
        node_mapping=vector(
            sample(node_metric("rp5", "node-exporter-pi5"), 1),
            sample(node_metric("vps", "node-exporter-vps"), 1),
            sample(node_metric("rp2", "node-exporter-pi2"), 1),
        ),
    )
    assert_no_addresses(out)
    assert "2 series" in out
    assert "Summary:" not in out


# --- 4.3 node_resource_trend ----------------------------------------------


@pytest.mark.parametrize("window", ["15m", "1h", "6h", "24h"])
def test_every_window_returns_a_summary_and_never_the_samples(window):
    values = [30.0 + i * 0.37 for i in range(61)]
    out = render(
        "node_resource_trend",
        {"node": "rp5", "resource": "cpu", "window": window},
        cpu=matrix(series(node_metric("rp5", "node-exporter-pi5"), values)),
    )
    assert "Summary:" in out
    assert window in out
    interior = [f"{v:.2f}" for v in values[1:-1]]
    leaked = [v for v in interior if v in out]
    assert leaked == [], f"raw samples reached the summary: {leaked}"
    # And the summary itself is the four figures plus a direction, not a dump.
    assert len(out.splitlines()) < 12


@pytest.mark.parametrize(
    "values,direction",
    [([10.0, 20.0, 30.0], "rising"), ([30.0, 20.0, 10.0], "falling"), ([7.0, 7.0, 7.0], "flat")],
)
def test_the_summary_states_the_direction_of_travel(values, direction):
    out = render(
        "node_resource_trend",
        {"node": "rp5", "resource": "load", "window": "1h"},
        load=matrix(series(node_metric("rp5", "node-exporter-pi5"), values)),
    )
    assert direction in out


def test_the_range_request_stays_within_the_configured_point_budget():
    plan = plan_query("node_resource_trend", {"node": "rp5", "resource": "cpu", "window": "24h"})
    assert plan.range_query is True


async def test_a_range_query_asks_prometheus_for_no_more_than_the_budget():
    router = Router({}, default=matrix(series(node_metric("rp5", "node-exporter-pi5"), [1.0, 2.0])))
    await tool(router, max_points=12).run(
        query_name="node_resource_trend", node="rp5", resource="cpu", window="24h"
    )
    params = router.requests[0].url.params
    span = float(params["end"]) - float(params["start"])
    assert span / float(params["step"]) + 1 <= 12


def test_disk_is_percent_free_against_its_own_bar_not_percent_used():
    out = render(
        "node_resource_trend",
        {"node": "vps", "resource": "disk", "window": "24h"},
        disk=matrix(series(node_metric("vps", "node-exporter-vps", mountpoint="/"), [30.0, 12.5])),
    )
    assert "free" in out
    assert "Compared against" in out and "15" in out
    assert "85" not in out, "the rule's form is percent free below 15, never 85 percent used"
    assert "mountpoint=/" in out or "/" in out
    # It crossed: 12.5% free is below the 15% bar.
    assert "crossed" in out.lower() or "below" in out.lower()


def test_disk_measures_the_root_filesystem_only():
    plan = plan_query("node_resource_trend", {"node": "vps", "resource": "disk", "window": "1h"})
    assert 'mountpoint="/"' in plan.expressions["disk"]


def test_swap_io_is_presented_as_the_primary_swap_signal():
    out = render(
        "node_resource_trend",
        {"node": "vps", "resource": "swap_io", "window": "24h"},
        swap_io=matrix(series(node_metric("vps", "node-exporter-vps"), [12.0, 83.42])),
    )
    assert "Compared against" in out and "50" in out and "pages/s" in out
    assert "primary" in out.lower()
    assert "crossed" in out.lower()


def test_swap_used_within_the_chronic_range_reads_as_normal_not_as_an_incident():
    # 84% full on the vps: below the 95% bar and inside the measured 64-90%
    # chronic range. The spec forbids presenting this as an approaching incident.
    out = render(
        "node_resource_trend",
        {"node": "vps", "resource": "swap_used", "window": "24h"},
        swap_used=matrix(series(node_metric("vps", "node-exporter-vps"), [77.0, 84.0])),
    )
    assert "not the rule's trigger" in out.lower()
    assert "pressure" in out.lower() and "fullness" in out.lower()
    assert "normal" in out.lower()
    assert "approaching" not in out.lower()
    assert "swap_io" in out, "the result must point at the signal that does trigger"


def test_memory_uses_the_pinned_75_bar_and_says_where_it_delivers():
    out = render(
        "node_resource_trend",
        {"node": "rp5", "resource": "memory", "window": "24h"},
        memory=matrix(series(node_metric("rp5", "node-exporter-pi5"), [52.0, 57.41])),
    )
    assert "Compared against 75.00 percent used" in out
    assert "90.00" not in out, (
        "90 is the non-delivering native bar and homelab_health's unsourced "
        "constant; the bar compared against must be the live 75"
    )
    assert "discord" in out.lower()


@pytest.mark.parametrize("resource", ["cpu", "load", "temperature"])
def test_resources_with_no_rule_carry_no_comparison(resource):
    out = render(
        "node_resource_trend",
        {"node": "rp5", "resource": resource, "window": "1h"},
        **{resource: matrix(series(node_metric("rp5", "node-exporter-pi5"), [10.0, 20.0]))},
    )
    assert "Compared against" not in out
    assert "Summary:" in out


def test_temperature_says_no_alert_exists_in_either_system():
    out = render(
        "node_resource_trend",
        {"node": "rp5", "resource": "temperature", "window": "1h"},
        temperature=matrix(
            series(node_metric("rp5", "node-exporter-pi5", type="cpu-thermal"), [48.0, 48.5])
        ),
    )
    assert "no alert" in out.lower()
    assert "either" in out.lower()


def test_an_empty_runtime_response_reads_as_not_derivable_not_as_a_measurement():
    # Decision §3.7's other half: the registry short-circuits the holes it knows
    # about, and a runtime-empty response for an in-domain request must land on
    # the same outcome rather than rendering as "measured, nothing there".
    out = render(
        "node_resource_trend",
        {"node": "rp2", "resource": "temperature", "window": "1h"},
        temperature=matrix(),
    )
    assert "not derivable" in out.lower() or "could not be derived" in out.lower()
    assert "outside" not in out.lower(), "this is not an out-of-domain rejection"
    assert "Summary:" not in out


# --- 4.4 scrape_targets ----------------------------------------------------


def _targets_payload(entries: list[tuple[str, str, str]]) -> dict:
    return {
        "status": "success",
        "data": {
            "activeTargets": [
                {
                    "labels": {"job": job, "instance": f"10.0.0.8:{9000 + n}"},
                    "scrapeUrl": f"http://10.0.0.8:{9000 + n}/metrics",
                    "globalUrl": f"http://10.0.0.8:{9000 + n}/metrics",
                    "health": health,
                    "lastError": error,
                }
                for n, (job, health, error) in enumerate(entries)
            ]
        },
    }


LIVE_JOBS = (
    "adguard-exporter",
    "cadvisor-pi5",
    "cadvisor-vps",
    "node-exporter-pi2",
    "node-exporter-pi5",
    "node-exporter-vps",
    "pushgateway",
)


def test_a_healthy_fleet_enumerates_every_target_with_its_value():
    # The count is whatever bare `up` returns — never a constant. Seven is what
    # the deployed scrape config has today (six named jobs plus pushgateway), and
    # asserting a literal six would fail against production.
    up = vector(*[sample({"job": job, "instance": f"10.0.0.8:{9000 + n}"}, 1) for n, job in enumerate(LIVE_JOBS)])
    out = render("scrape_targets", {}, up=up, up_over_window=up, targets=_targets_payload([(j, "up", "") for j in LIVE_JOBS]))
    assert_no_addresses(out)
    for job in LIVE_JOBS:
        assert job in out
    assert str(len(LIVE_JOBS)) in out
    assert "up" in out.lower()


def test_the_enumerated_count_follows_the_response_and_is_not_hardcoded():
    jobs = LIVE_JOBS + ("an-eighth-job",)
    up = vector(*[sample({"job": job}, 1) for job in jobs])
    out = render("scrape_targets", {}, up=up, up_over_window=up, targets=_targets_payload([(j, "up", "") for j in jobs]))
    assert "8" in out and "an-eighth-job" in out


def test_a_down_target_carries_its_scrape_error():
    up = vector(sample({"job": "cadvisor-pi5"}, 1), sample({"job": "pushgateway"}, 0))
    out = render(
        "scrape_targets",
        {},
        up=up,
        up_over_window=vector(sample({"job": "cadvisor-pi5"}, 1), sample({"job": "pushgateway"}, 1)),
        targets=_targets_payload(
            [("cadvisor-pi5", "up", ""), ("pushgateway", "down", "context deadline exceeded")]
        ),
    )
    assert "pushgateway" in out
    assert "context deadline exceeded" in out
    assert "DOWN" in out


def test_a_target_never_up_in_the_window_is_bounded_not_invented():
    up = vector(sample({"job": "node-exporter-pi2"}, 0))
    out = render(
        "scrape_targets",
        {},
        up=up,
        # max_over_time(up[24h]) == 0 → not up at any point inside the window.
        up_over_window=vector(sample({"job": "node-exporter-pi2"}, 0)),
        targets=_targets_payload([("node-exporter-pi2", "down", "connection refused")]),
    )
    assert "longer than" in out and "24h" in out
    # No invented duration: nothing that reads as "down for N hours/minutes".
    assert not re.search(r"down for \d", out, re.IGNORECASE)
    assert "since" not in out.lower() or "no specific duration" in out.lower()


def test_a_target_down_now_but_up_inside_the_window_states_no_duration_either():
    up = vector(sample({"job": "cadvisor-vps"}, 0))
    out = render(
        "scrape_targets",
        {},
        up=up,
        up_over_window=vector(sample({"job": "cadvisor-vps"}, 1)),
        targets=_targets_payload([("cadvisor-vps", "down", "connection refused")]),
    )
    assert "within the last 24h" in out or "inside the window" in out
    assert not re.search(r"down for \d", out, re.IGNORECASE)


def test_scrape_targets_reports_an_empty_up_response_as_not_derivable():
    out = render("scrape_targets", {}, up=vector(), up_over_window=vector(), targets=_targets_payload([]))
    assert "not derivable" in out.lower() or "could not be derived" in out.lower()


# --- 4.5 endpoint_history: discovery, refusal, refresh, encoding -----------

DISCOVERED = ("core-services_web-front", "backups_nightly-dump")

BULK_STATUSES = [
    {"name": "Web Front", "group": "Core Services", "key": "core-services_web-front", "results": []},
    {"name": "Nightly Dump", "group": "Backups", "key": "backups_nightly-dump", "results": []},
]


def _statuses_payload(key: str = "core-services_web-front") -> dict:
    return {
        "name": "Web Front",
        "group": "Core Services",
        "key": key,
        "results": [
            {
                "timestamp": "2026-09-01T09:00:00.000000000Z",
                "success": False,
                "duration": 431_000_000,
                "status": 502,
                # An address-bearing field on the results, per the probe note.
                "hostname": "web.example.invalid",
                "conditionResults": [
                    {"condition": "[STATUS] == 200", "success": False},
                    {"condition": "[RESPONSE_TIME] < 500", "success": True},
                ],
            }
        ],
        "events": [
            {"timestamp": "2026-08-12T02:00:00.000000000Z", "type": "START"},
            {"timestamp": "2026-08-12T02:01:00.000000000Z", "type": "HEALTHY"},
            {"timestamp": "2026-08-30T21:14:00.000000000Z", "type": "UNHEALTHY"},
        ],
    }


async def test_construction_makes_no_network_call():
    router = Router({}, default={})
    tool(router)
    assert router.requests == [], "discovery is at first use, never at construction"


async def test_discovery_runs_at_first_use_and_is_memoized():
    router = Router(
        {
            "/api/v1/endpoints/statuses": BULK_STATUSES,
            "/statuses": _statuses_payload(),
            "/uptimes/": 0.9993,
        }
    )
    subject = tool(router)
    for _ in range(2):
        result = await subject.run(
            query_name="endpoint_history", endpoint="core-services_web-front", window="24h"
        )
        assert result.ok is True, result.error
    discoveries = [p for p in router.paths if p.endswith("/api/v1/endpoints/statuses")]
    assert len(discoveries) == 1, "the discovered set is memoized, not refetched per call"


async def test_an_undiscovered_key_is_refused_and_never_reaches_a_route():
    router = Router({"/api/v1/endpoints/statuses": BULK_STATUSES})
    subject = tool(router)
    result = await subject.run(query_name="endpoint_history", endpoint="core_absent", window="24h")
    assert result.ok is False
    assert "core_absent" in result.error
    assert not any("core_absent" in path for path in router.paths)


async def test_a_renamed_endpoint_becomes_queryable_without_a_restart():
    state = {"keys": list(BULK_STATUSES)}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/v1/endpoints/statuses"):
            return httpx.Response(200, json=state["keys"])
        if path.endswith("/statuses"):
            return httpx.Response(200, json=_statuses_payload("core-services_web-frontend"))
        return httpx.Response(200, json=0.99)

    subject = tool(handler)
    first = await subject.run(
        query_name="endpoint_history", endpoint="core-services_web-front", window="24h"
    )
    assert first.ok is True, first.error

    # The owner renames the endpoint while the process runs.
    state["keys"] = [
        {
            "name": "Web Frontend",
            "group": "Core Services",
            "key": "core-services_web-frontend",
            "results": [],
        }
    ]
    renamed = await subject.run(
        query_name="endpoint_history", endpoint="core-services_web-frontend", window="24h"
    )
    assert renamed.ok is True, (
        "a lookup miss must refresh the discovered set; the new key is queryable "
        "without restarting the process"
    )


async def test_discovery_failure_fails_closed_with_an_explicit_error():
    router = Router({"/api/v1/endpoints/statuses": 503})
    subject = tool(router)
    result = await subject.run(
        query_name="endpoint_history", endpoint="core-services_web-front", window="24h"
    )
    assert result.ok is False
    assert "Gatus" in result.error
    assert "503" in result.error or "discover" in result.error.lower()
    # Fail closed: the argument is never passed through to a backend route.
    assert not any("web-front" in path for path in router.paths)


async def test_a_stale_key_set_is_not_served_when_the_refresh_fails():
    # Failing closed means failing closed even when a previously discovered set
    # is sitting in memory: the spec's requirement is about the INVOCATION, and
    # serving a remembered set would answer from a snapshot of a backend that is
    # currently unreachable.
    router = Router({"/api/v1/endpoints/statuses": 503})
    subject = tool(router, discovered=("core-services_web-front",), ttl=0.0)
    result = await subject.run(
        query_name="endpoint_history", endpoint="core-services_web-front", window="24h"
    )
    assert result.ok is False
    assert "Gatus" in result.error and "discovery" in result.error.lower()
    assert not any("web-front" in path for path in router.paths)


def test_an_unsafe_discovered_key_is_percent_encoded_into_the_route():
    unsafe = "core_a b/../c"
    plan = plan_query(
        "endpoint_history",
        {"endpoint": unsafe, "window": "1h"},
        discovered={"endpoint": (unsafe,)},
    )
    for route in plan.routes.values():
        assert "/../" not in route and " " not in route
        assert "%2F" in route or "%20" in route


def test_endpoint_history_answers_since_when_from_events_not_from_results():
    out = render(
        "endpoint_history",
        {"endpoint": "core-services_web-front", "window": "24h"},
        discovered={"endpoint": DISCOVERED},
        statuses=_statuses_payload(),
        uptime=0.9993,
    )
    assert_no_addresses(out)
    # The last UNHEALTHY transition, from `events`. `results` holds only ~1.7h,
    # so its oldest entry can never answer "since when".
    # The "since" line must come from the transition events. Asserting on that
    # line specifically, not on the whole result: the last CHECK's timestamp is
    # legitimate elsewhere in the summary, and it is only as an answer to "since
    # when" that it would be confidently wrong.
    since = [line for line in out.splitlines() if line.startswith("Since:")]
    assert since, out
    assert "2026-08-30" in since[0], "the last UNHEALTHY transition, from `events`"
    assert "2026-09-01" not in since[0], (
        "`results` holds only ~1.7h, so it can never answer 'since when'"
    )
    assert "99.93" in out or "0.9993" in out
    assert "[STATUS] == 200" in out, "the failing condition is what the owner asks for"


def test_endpoint_history_tolerates_a_null_results_page():
    # A page past the end returns JSON null, not []. `len(None)` would raise.
    payload = _statuses_payload()
    payload["results"] = None
    out = render(
        "endpoint_history",
        {"endpoint": "core-services_web-front", "window": "1h"},
        discovered={"endpoint": DISCOVERED},
        statuses=payload,
        uptime=1.0,
    )
    assert "2026-08-30" in out


def test_endpoint_history_with_no_events_states_that_rather_than_inventing_one():
    payload = _statuses_payload()
    payload["events"] = []
    out = render(
        "endpoint_history",
        {"endpoint": "core-services_web-front", "window": "1h"},
        discovered={"endpoint": DISCOVERED},
        statuses=payload,
        uptime=1.0,
    )
    assert "no transition" in out.lower()
    assert "2026-08" not in out


# --- 4.6 An arriving event resolves to a queryable argument ----------------


def test_a_gatus_event_composes_the_key_the_backend_uses():
    event = Event(
        id="e1",
        title="Gatus: Core Services/Web Front",
        message="An alert has been triggered",
        arrival_time=NOW,
    )
    identity = derive_identity(event)
    assert identity.source == "gatus"
    resolved = resolve_endpoint_key(identity.name, DISCOVERED)
    assert resolved == "core-services_web-front"


def test_the_key_composition_matches_the_measured_shape():
    # `sanitize(lower(group)) + "_" + sanitize(lower(name))`, a 1:1 character
    # substitution — the key length equals the composed source's length.
    key = compose_gatus_key("Core Services", "Web Front")
    assert key == "core-services_web-front"
    assert len(key) == len("core services" + "_" + "web front")
    assert compose_gatus_key("Media", "Plex.tv") == "media_plex-tv"


async def test_an_arriving_event_yields_a_queryable_argument_without_guesswork():
    router = Router(
        {
            "/api/v1/endpoints/statuses": BULK_STATUSES,
            "/statuses": _statuses_payload(),
            "/uptimes/": 0.99,
        }
    )
    identity = derive_identity(
        Event(id="e1", title="Gatus: Core Services/Web Front", message="triggered", arrival_time=NOW)
    )
    result = await tool(router).run(
        query_name="endpoint_history", endpoint=identity.name, window="24h"
    )
    assert result.ok is True, result.error
    assert any("core-services_web-front" in path for path in router.paths)


async def test_an_unresolvable_event_name_is_reported_not_queried():
    router = Router({"/api/v1/endpoints/statuses": BULK_STATUSES})
    identity = derive_identity(
        Event(id="e1", title="Gatus: Retired Group/Gone", message="triggered", arrival_time=NOW)
    )
    result = await tool(router).run(
        query_name="endpoint_history", endpoint=identity.name, window="24h"
    )
    assert result.ok is False
    assert "Retired Group/Gone" in result.error or "retired-group_gone" in result.error
    assert not any("gone" in path.lower() for path in router.paths)


def test_resolution_never_invents_a_key_outside_the_discovered_set():
    assert resolve_endpoint_key("anything/at-all", DISCOVERED) is None
    assert resolve_endpoint_key("../../health", DISCOVERED) is None
    assert resolve_endpoint_key("backups_nightly-dump", DISCOVERED) == "backups_nightly-dump"


# --- 4.7 freshness_check ---------------------------------------------------

FRESHNESS_METRICS = QUERY_REGISTRY["freshness_check"]


def _freshness_payload(*, at: float = NOW, written: float = NOW - 3600.0) -> dict:
    return vector(
        sample(
            {
                "__name__": "homelab_backup_last_success_timestamp",
                "job": "node-exporter-pi5",
                "instance": NODE_INSTANCE["rp5"],
                "direction": "push",
            },
            written,
            at=at,
        ),
        sample(
            {
                "__name__": "health_etl_last_success_timestamp_seconds",
                "job": "node-exporter-vps",
                "instance": NODE_INSTANCE["vps"],
            },
            written,
            at=at,
        ),
    )


def test_freshness_reports_the_raw_timestamp_beside_the_derived_age():
    out = render("freshness_check", {}, timestamps=_freshness_payload())
    assert_no_addresses(out)
    assert str(int(NOW - 3600.0)) in out, "the RAW timestamp must be present"
    assert "1.0" in out and "h" in out
    assert "homelab_backup_last_success_timestamp" in out
    assert "health_etl_last_success_timestamp_seconds" in out
    assert "rp5" in out and "vps" in out
    assert "direction=push" in out


def _ages(text: str) -> list[float]:
    return [float(m) for m in re.findall(r"([\d.]+)\s*h ago", text)]


def test_a_frozen_writer_surfaces_as_a_growing_age():
    frozen = NOW - 3600.0
    first = render("freshness_check", {}, timestamps=_freshness_payload(at=NOW, written=frozen))
    later = render(
        "freshness_check", {}, timestamps=_freshness_payload(at=NOW + 7200.0, written=frozen)
    )
    assert _ages(first) and _ages(later)
    assert all(b > a for a, b in zip(_ages(first), _ages(later))), (
        "a dead writer freezes the raw timestamp, so the derived age must grow"
    )
    # And the raw timestamp is unchanged across the two, which is the evidence.
    assert str(int(frozen)) in first and str(int(frozen)) in later


def test_freshness_derives_the_age_rather_than_reading_a_precomputed_one():
    plan = plan_query("freshness_check", {})
    expression = plan.expressions["timestamps"]
    assert "_age" not in expression and "time()" not in expression
    for metric in ("homelab_backup_last_success_timestamp", "health_etl_last_success_timestamp_seconds"):
        assert metric in expression


def test_freshness_reports_an_empty_response_as_not_derivable():
    out = render("freshness_check", {}, timestamps=vector())
    assert "not derivable" in out.lower() or "could not be derived" in out.lower()


# --- 4.8 container_state ---------------------------------------------------


def _container_payloads(node: str, job: str, *, health: bool) -> dict:
    def four(name: str, last_seen: float, created: float, oom: float) -> list[dict]:
        labels = {"job": job, "instance": NODE_INSTANCE[node], "name": name}
        return [
            sample(labels, last_seen),
            sample(labels, created),
            sample(labels, oom),
        ]

    gatus_last, gatus_created, gatus_oom = four("gatus", NOW - 12.0, NOW - 400_000.0, 0)
    henk_last, henk_created, henk_oom = four("henk", NOW - 9.0, NOW - 90_000.0, 2)
    payloads = {
        "last_seen": vector(gatus_last, henk_last),
        "created": vector(gatus_created, henk_created),
        "oom_events": vector(gatus_oom, henk_oom),
    }
    payloads["health_state"] = (
        vector(
            sample({"job": job, "instance": NODE_INSTANCE[node], "name": "gatus"}, 1),
            sample({"job": job, "instance": NODE_INSTANCE[node], "name": "henk"}, 1),
        )
        if health
        else vector()
    )
    return payloads


def test_container_state_labels_start_time_as_creation_and_disclaims_restarts():
    out = render(
        "container_state",
        {"node": "vps"},
        **_container_payloads("vps", "cadvisor-vps", health=True),
    )
    assert_no_addresses(out)
    assert "created" in out.lower()
    assert "restart" in out.lower() and "not observable" in out.lower()
    assert "last start" in out.lower()
    assert "gatus" in out and "henk" in out


def test_container_state_declares_its_omission_semantics():
    out = render(
        "container_state",
        {"node": "vps"},
        **_container_payloads("vps", "cadvisor-vps", health=True),
    )
    lowered = out.lower()
    assert "stopped or removed" in lowered
    assert "absent" in lowered


def test_container_state_on_rp5_says_the_health_column_is_unavailable():
    out = render(
        "container_state",
        {"node": "rp5"},
        **_container_payloads("rp5", "cadvisor-pi5", health=False),
    )
    assert "health" in out.lower() and "unavailable" in out.lower()
    # The rest of the query still answers.
    assert "gatus" in out and "OOM" in out.upper()


def test_a_named_container_absent_from_the_backend_returns_a_reading():
    # cadvisor drops a stopped container's series entirely, so the template uses
    # the fleet's own `or vector()` idiom: absence must yield a value.
    expression = named_container_expression("vps", "mollysocket", known=("gatus", "mollysocket"))
    assert "or vector(" in expression
    assert 'name="mollysocket"' in expression
    absent = vector(sample({}, 0))
    reading = render_named_container_reading("vps", "mollysocket", absent)
    assert "mollysocket" in reading
    assert "stopped or removed" in reading.lower()
    assert reading.strip() != ""


def test_a_named_container_is_fillable_only_from_a_discovered_name():
    with pytest.raises(QueryRefused):
        named_container_expression("vps", "anything-the-model-typed", known=("gatus",))
    with pytest.raises(QueryRefused):
        named_container_expression("rp2", "gatus", known=("gatus",))


def test_container_state_reports_an_empty_response_as_not_derivable():
    out = render(
        "container_state",
        {"node": "vps"},
        last_seen=vector(),
        created=vector(),
        oom_events=vector(),
        health_state=vector(),
    )
    assert "not derivable" in out.lower() or "could not be derived" in out.lower()


# --- 4.9 dns_performance ---------------------------------------------------


def _dns_payloads(node: str, values: list[float], *, mapped: tuple[str, ...] = ("rp5", "vps", "rp2")):
    from henk.tools.query_registry import NODE_EXPORTER_JOBS

    return {
        "series": matrix(
            *[
                series(
                    {
                        "job": "adguard-exporter",
                        "instance": ADGUARD_INSTANCE,
                        "server": ADGUARD_SERVER[n],
                    },
                    values if n == node else [0.001, 0.001],
                )
                for n in ("rp5", "vps", "rp2")
            ]
        ),
        "node_mapping": vector(
            *[sample(node_metric(n, NODE_EXPORTER_JOBS[n]), 1) for n in mapped]
        ),
    }


def test_dns_maps_a_node_to_its_series_by_derivation_and_renders_no_address():
    out = render(
        "dns_performance",
        {"node": "rp2", "window": "24h"},
        **_dns_payloads("rp2", [0.0724, 0.0747]),
    )
    assert_no_addresses(out)
    assert "rp2" in out
    assert "derived" in out.lower()
    assert "74.7" in out or "74.70" in out


def test_dns_compares_against_the_measured_baseline_and_the_nodes_own_bars():
    out = render(
        "dns_performance",
        {"node": "rp2", "window": "24h"},
        **_dns_payloads("rp2", [0.0724, 0.0747]),
    )
    assert "74.72" in out, "the measured 24h baseline for rp2"
    assert "200" in out, "rp2's own warning bar, not another node's"
    assert "60 ms" not in out and "80 ms" not in out, "another node's bars must not appear"


def test_dns_carries_the_rolling_average_caveat():
    out = render(
        "dns_performance",
        {"node": "rp5", "window": "15m"},
        **_dns_payloads("rp5", [0.002439, 0.002439]),
    )
    assert "rolling average" in out.lower() or "cumulative" in out.lower()
    assert "flat" in out.lower()


def test_an_underivable_node_is_reported_rather_than_returned_empty():
    # rp2's node-exporter is not reporting, so its host cannot be matched to an
    # AdGuard series. That is D2's third outcome, not an empty measurement.
    out = render(
        "dns_performance",
        {"node": "rp2", "window": "24h"},
        **_dns_payloads("rp2", [0.07, 0.07], mapped=("rp5", "vps")),
    )
    assert_no_addresses(out)
    assert "could not be derived" in out.lower() or "not derivable" in out.lower()
    assert "rp2" in out
    assert "Summary:" not in out
    assert "outside" not in out.lower()
    # And it names the condition it actually found. The two underivable
    # conditions are different diagnoses and the owner acts differently on each:
    # "this node is not reporting at all" sends them to the host, while "no
    # AdGuard series belongs to it" sends them to the exporter's configuration.
    assert "node-exporter" in out and "absent" in out.lower()


def test_a_reporting_node_with_no_adguard_series_is_a_different_diagnosis():
    payloads = _dns_payloads("rp2", [0.07, 0.07])
    # The node reports, but no AdGuard series carries its host.
    payloads["series"]["data"]["result"] = [
        series(
            {"job": "adguard-exporter", "instance": ADGUARD_INSTANCE, "server": ADGUARD_SERVER["rp5"]},
            [0.0024, 0.0024],
        )
    ]
    out = render("dns_performance", {"node": "rp2", "window": "24h"}, **payloads)
    assert "could not be derived" in out.lower()
    assert "matched" in out.lower()
    assert "absent" not in out.lower(), (
        "rp2 IS reporting here; saying its series are absent would send the "
        "owner to the wrong host"
    )


def test_the_dns_mapping_is_not_stored_anywhere():
    entry = QUERY_REGISTRY["dns_performance"]
    assert entry.derives_node_mapping is True
    assert 'up{job=~"node-exporter.*"}' in entry.expressions["node_mapping"]
    for template in entry.templates.values():
        assert "server" not in template


# --- 4.10 Honest failure ---------------------------------------------------


async def test_a_backend_timeout_names_the_backend_and_the_cause():
    router = Router({"/api/v1/query": httpx.ReadTimeout("timed out")})
    result = await tool(router).run(query_name="freshness_check")
    assert result.ok is False
    assert "Prometheus" in result.error
    assert "timed out" in result.error.lower()


async def test_a_non_2xx_response_names_the_backend_and_the_status():
    router = Router({"/api/v1/query": 500})
    result = await tool(router).run(query_name="freshness_check")
    assert result.ok is False
    assert "Prometheus" in result.error and "500" in result.error


async def test_a_gatus_failure_names_gatus_not_prometheus():
    router = Router({"/api/v1/endpoints/statuses": BULK_STATUSES, "/statuses": 502})
    result = await tool(router).run(
        query_name="endpoint_history", endpoint="core-services_web-front", window="24h"
    )
    assert result.ok is False
    assert "Gatus" in result.error and "502" in result.error


async def test_a_partial_failure_is_never_presented_as_a_whole_result():
    # `container_state` issues four requests. If the third fails, the tool must
    # not render the first two as though they were the answer.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] >= 3:
            return httpx.Response(503, json={})
        return httpx.Response(200, json=vector(sample({"job": "cadvisor-vps", "name": "gatus"}, NOW)))

    result = await tool(handler).run(query_name="container_state", node="vps")
    assert result.ok is False
    assert "Prometheus" in result.error and "503" in result.error
    assert "gatus" not in (result.content or "")


async def test_malformed_json_fails_honestly_rather_than_crashing_the_turn():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})

    result = await tool(handler).run(query_name="freshness_check")
    assert result.ok is False
    assert "Prometheus" in result.error


async def test_an_accepted_query_renders_a_summary_end_to_end():
    up = vector(sample({"job": "cadvisor-pi5", "instance": NODE_INSTANCE["rp5"]}, 1))
    router = Router(
        {
            "/api/v1/query": up,
            "/api/v1/targets": _targets_payload([("cadvisor-pi5", "up", "")]),
        }
    )
    result = await tool(router).run(query_name="scrape_targets")
    assert result.ok is True, result.error
    assert "cadvisor-pi5" in result.content
    assert_no_addresses(result.content)
