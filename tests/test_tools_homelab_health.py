"""homelab_health tests (task 4.1, amended by §10), from specs/homelab-tools.

§10 amends the tool so that it cannot disagree with `node_resource_trend`: its
bars are the registry's `Threshold` objects rather than three hardcoded
constants, and its output is projected the same way — nodes named by their
friendly enum value, no `instance` label anywhere.

**Every Prometheus fixture here carries an address-shaped `instance` label by
construction**, the same discipline `test_query_renderers` uses: a projection is
only proved when the payload contains something to drop. All of it is placeholder
(`10.0.0.x`), never a real backend address.
"""

from __future__ import annotations

import httpx
import pytest

from henk.tools import homelab_health as health_module
from henk.tools.base import ToolClass
from henk.tools.homelab_health import HomelabHealthTool
from henk.tools.query_registry import QUERY_REGISTRY, plan_query

TREND_THRESHOLDS = QUERY_REGISTRY["node_resource_trend"].thresholds

PROMETHEUS = "http://10.0.0.1:9090"
GATUS = "http://10.0.0.2:8080"

#: Every address a fixture may carry. Assertions sweep the whole list, so a leak
#: through any one of them fails somewhere.
PLACEHOLDER_ADDRESSES = ("10.0.0.1", "10.0.0.2", "10.0.0.5", "10.0.0.6")

NODE_INSTANCE = {"rp5": "10.0.0.5:9100", "vps": "10.0.0.6:9100"}
NODE_JOB = {"rp5": "node-exporter-pi5", "vps": "node-exporter-vps"}


def assert_no_addresses(text: str) -> None:
    for address in PLACEHOLDER_ADDRESSES:
        assert address not in text, f"{address} leaked into homelab_health output"


def _gatus(all_up: bool):
    return [
        {"group": "core", "name": "gatus-web", "key": "core_gatus-web",
         "results": [{"success": True, "hostname": "10.0.0.2"}]},
        {"group": "core", "name": "taiga", "key": "core_taiga",
         "results": [{"success": all_up, "hostname": "10.0.0.2"}]},
    ]


def _resource_of(query: str) -> str:
    """Which resource a PromQL expression measures, for the mock transport."""
    if "MemAvailable" in query:
        return "memory"
    if "filesystem" in query:
        return "disk"
    return "load"


def _prom_value(query: str, memory: float, disk: float, load: float):
    """One sample per node, labelled with BOTH `job` and an address `instance`."""
    value = {"memory": memory, "disk": disk, "load": load}[_resource_of(query)]
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"job": NODE_JOB[node], "instance": NODE_INSTANCE[node]},
                    "value": [0, str(value)],
                }
                for node in ("rp5", "vps")
            ],
        },
    }


def _make_tool(handler) -> HomelabHealthTool:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HomelabHealthTool(client, gatus_url=GATUS, prometheus_url=PROMETHEUS)


def _handler(*, all_up: bool = True, memory: float = 40.0, disk: float = 50.0, load: float = 1.0):
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            return httpx.Response(200, json=_gatus(all_up))
        return httpx.Response(
            200, json=_prom_value(request.url.params["query"], memory, disk, load)
        )

    return handler


async def _health(**kwargs) -> str:
    result = await _make_tool(_handler(**kwargs))._run()
    assert result.ok
    return result.content


def _trend(resource: str, value: float, node: str = "rp5") -> str:
    """`node_resource_trend`'s own rendering of one flat reading.

    Built here rather than imported from the renderer tests so the comparison
    below exercises the shipping renderer against the shipping health tool, with
    no shared test helper standing between them.
    """
    plan = plan_query(
        "node_resource_trend", {"node": node, "resource": resource, "window": "1h"}
    )
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"job": NODE_JOB[node], "instance": NODE_INSTANCE[node]},
                    "values": [[0, str(value)], [600, str(value)]],
                }
            ],
        },
    }
    return QUERY_REGISTRY["node_resource_trend"].renderer(plan, {resource: payload})


# --- 10.1 The two tools cannot disagree about a threshold ------------------


@pytest.mark.parametrize("resource", ["memory", "disk"])
def test_both_tools_read_the_same_threshold_object_not_a_copy(resource):
    # Identity, not equality: two equal copies of one bar can be edited apart,
    # which is exactly how the deployed 90 drifted from the live 75.
    assert health_module.THRESHOLDS[resource] is TREND_THRESHOLDS[resource]


def test_homelab_health_declares_no_bar_the_registry_does_not_pin():
    assert set(health_module.THRESHOLDS) <= set(TREND_THRESHOLDS)


@pytest.mark.parametrize(
    "resource, crossing, clear",
    [
        # memory: percent USED, above 75. Just over and just under the live bar.
        ("memory", 75.5, 74.5),
        # disk: percent FREE, below 15 — the polarity a copied ">" gets backwards.
        ("disk", 14.5, 15.5),
    ],
)
async def test_a_reading_either_side_of_the_bar_agrees_across_both_tools(
    resource, crossing, clear
):
    bar = TREND_THRESHOLDS[resource]
    assert bar.crossed_by(crossing) and not bar.crossed_by(clear)

    over = await _health(**{resource: crossing})
    under = await _health(**{resource: clear})

    assert "crossed" in over and "DEGRADED" in over
    assert "crossed" not in under
    assert "DEGRADED" not in under

    assert "crossed" in _trend(resource, crossing)
    assert "stayed clear of" in _trend(resource, clear)


async def test_the_memory_bar_is_the_live_75_and_the_retired_90_is_gone():
    # 80% used: healthy under the retired 90, a crossing under the live 75. This
    # is the whole user-visible delta, asserted on the value that discriminates.
    content = await _health(memory=80.0)
    assert "DEGRADED" in content
    assert "75.00" in content and "90" not in content
    assert "High memory usage" in content, "the crossing must name the rule that pins it"


async def test_disk_is_percent_free_below_its_bar_not_percent_used():
    # 10 reads as 10% FREE — a crossing. Under the retired "10% used vs 90"
    # reading this was the healthiest node in the fleet.
    content = await _health(disk=10.0)
    assert "DEGRADED" in content
    assert "free" in content
    assert "HenkDiskPressure" in content


async def test_load_is_reported_without_a_verdict_because_no_rule_defines_a_bar():
    # The retired 8.0 constant was unsourced: no rule exists in either alerting
    # system. A figure far above it must still not read as an incident.
    content = await _health(load=42.0)
    assert "42" in content
    assert "DEGRADED" not in content
    assert "load" in content and "no alert rule" in content.lower()


def test_the_health_expressions_are_the_registry_expressions_widened_by_job():
    trend = QUERY_REGISTRY["node_resource_trend"].expressions
    for resource, expression in health_module.QUERIES.items():
        assert expression == trend[resource].replace(
            'job="<job>"', health_module.FLEET_JOB_SELECTOR
        )
        assert "<job>" not in expression
        assert 'instance="' not in expression


# --- 10.2 No raw `instance` value reaches the output -----------------------


async def test_no_instance_label_reaches_the_summary_nodes_are_named_by_enum():
    content = await _health()
    assert_no_addresses(content)
    assert "rp5" in content and "vps" in content


async def test_no_instance_label_reaches_a_degraded_summary_either():
    content = await _health(all_up=False, memory=99.0, disk=1.0)
    assert_no_addresses(content)
    assert "rp5" in content and "vps" in content


async def test_a_backend_error_string_carrying_the_url_is_scrubbed():
    # httpx's HTTPStatusError quotes the request URL, and the deployed base URLs
    # ARE tailnet addresses — so the unreachable line is an address path too.
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=_prom_value(request.url.params["query"], 40, 50, 1.0))

    result = await _make_tool(handler)._run()
    assert "source unreachable" in result.content
    assert_no_addresses(result.content)


async def test_a_sample_without_a_job_label_is_not_named_by_its_instance():
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            return httpx.Response(200, json=_gatus(True))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"instance": "10.0.0.5:9100"}, "value": [0, "40"]}
                    ],
                },
            },
        )

    result = await _make_tool(handler)._run()
    assert_no_addresses(result.content)


# --- The original scenarios, on the amended shape --------------------------


async def test_healthy_homelab_summarized():
    content = await _health()
    assert "healthy" in content
    assert "rp5" in content
    assert "DOWN" not in content


async def test_degraded_service_reported():
    content = await _health(all_up=False)
    assert "DOWN" in content
    assert "taiga" in content
    assert "DEGRADED" in content


async def test_node_metric_beyond_threshold_named():
    # The bar is read from the registry rather than written here: an old test
    # that encoded 90 as a literal was the second uncontrolled copy.
    bar = TREND_THRESHOLDS["memory"]
    content = await _health(memory=bar.value + 5.0)
    assert "DEGRADED" in content
    assert "memory" in content


async def test_backend_unreachable_is_explicit_not_fabricated():
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=_prom_value(request.url.params["query"], 40, 50, 1.0))

    result = await _make_tool(handler)._run()
    assert "source unreachable" in result.content  # Gatus
    assert "rp5" in result.content  # Prometheus data still returned


async def test_prometheus_unreachable_still_returns_gatus():
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            return httpx.Response(200, json=_gatus(True))
        raise httpx.ConnectError("refused", request=request)

    result = await _make_tool(handler)._run()
    assert "endpoints healthy" in result.content
    assert "Prometheus: source unreachable" in result.content


async def test_prometheus_partial_data_is_kept_not_discarded():
    # The first PromQL query (memory) succeeds; a later one fails. The real
    # memory data must still be reported alongside the unreachable note.
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            return httpx.Response(200, json=_gatus(True))
        query = request.url.params["query"]
        if "MemAvailable" in query:
            return httpx.Response(200, json=_prom_value(query, 40, 50, 1.0))
        raise httpx.ConnectError("refused", request=request)

    result = await _make_tool(handler)._run()
    assert "rp5" in result.content  # partial (memory) data retained
    assert "Prometheus: source unreachable" in result.content  # failure surfaced


async def test_endpoints_are_named_by_the_gatus_key_so_two_groups_cannot_merge():
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            return httpx.Response(
                200,
                json=[
                    {"group": "core", "name": "taiga", "key": "core_taiga",
                     "results": [{"success": False}]},
                    {"group": "edge", "name": "taiga", "key": "edge_taiga",
                     "results": [{"success": False}]},
                ],
            )
        return httpx.Response(200, json=_prom_value(request.url.params["query"], 40, 50, 1.0))

    result = await _make_tool(handler)._run()
    assert "core_taiga" in result.content
    assert "edge_taiga" in result.content


def test_classification_is_read_only():
    assert HomelabHealthTool.tool_class is ToolClass.READ_ONLY


def test_no_threshold_can_be_supplied_at_construction():
    # The deployed drift existed because the bar was a constructor default that
    # nothing overrode. An override parameter would let it drift again.
    import inspect

    parameters = inspect.signature(HomelabHealthTool.__init__).parameters
    assert not [name for name in parameters if "threshold" in name]
