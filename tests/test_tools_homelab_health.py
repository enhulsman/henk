"""homelab_health tests (task 4.1), from specs/homelab-tools."""

from __future__ import annotations

import httpx

from henk.tools.base import ToolClass
from henk.tools.homelab_health import HomelabHealthTool


def _gatus(all_up: bool):
    return [
        {"name": "gatus-web", "results": [{"success": True}]},
        {"name": "taiga", "results": [{"success": all_up}]},
    ]


def _prom_value(query: str, mem: float, disk: float, load: float):
    if "MemAvailable" in query:
        v = mem
    elif "filesystem" in query:
        v = disk
    else:
        v = load
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"instance": "rp5:9100"}, "value": [0, str(v)]},
                {"metric": {"instance": "vps:9100"}, "value": [0, str(v)]},
            ],
        },
    }


def _make_tool(handler) -> HomelabHealthTool:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HomelabHealthTool(
        client, gatus_url="http://rp5:8080", prometheus_url="http://vps:9090"
    )


async def test_healthy_homelab_summarized():
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            return httpx.Response(200, json=_gatus(all_up=True))
        return httpx.Response(200, json=_prom_value(request.url.params["query"], 40, 50, 1.0))

    result = await _make_tool(handler)._run()
    assert result.ok
    assert "healthy" in result.content
    assert "rp5:9100" in result.content
    assert "DOWN" not in result.content


async def test_degraded_service_reported():
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            return httpx.Response(200, json=_gatus(all_up=False))
        return httpx.Response(200, json=_prom_value(request.url.params["query"], 40, 50, 1.0))

    result = await _make_tool(handler)._run()
    assert result.ok
    assert "DOWN" in result.content
    assert "taiga" in result.content
    assert "DEGRADED" in result.content


async def test_node_metric_beyond_threshold_named():
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            return httpx.Response(200, json=_gatus(all_up=True))
        # 95% memory is beyond the 90% threshold.
        return httpx.Response(200, json=_prom_value(request.url.params["query"], 95, 50, 1.0))

    result = await _make_tool(handler)._run()
    assert "DEGRADED" in result.content
    assert "memory" in result.content


async def test_backend_unreachable_is_explicit_not_fabricated():
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=_prom_value(request.url.params["query"], 40, 50, 1.0))

    result = await _make_tool(handler)._run()
    assert "source unreachable" in result.content  # Gatus
    assert "rp5:9100" in result.content  # Prometheus data still returned


async def test_prometheus_unreachable_still_returns_gatus():
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            return httpx.Response(200, json=_gatus(all_up=True))
        raise httpx.ConnectError("refused", request=request)

    result = await _make_tool(handler)._run()
    assert "endpoints healthy" in result.content
    assert "Prometheus: source unreachable" in result.content


async def test_prometheus_partial_data_is_kept_not_discarded():
    # The first PromQL query (memory) succeeds; a later one fails. The real
    # memory data must still be reported alongside the unreachable note.
    def handler(request):
        if request.url.path == "/api/v1/endpoints/statuses":
            return httpx.Response(200, json=_gatus(all_up=True))
        query = request.url.params["query"]
        if "MemAvailable" in query:
            return httpx.Response(200, json=_prom_value(query, 40, 50, 1.0))
        raise httpx.ConnectError("refused", request=request)

    result = await _make_tool(handler)._run()
    assert "rp5:9100" in result.content  # partial (memory) data retained
    assert "Prometheus: source unreachable" in result.content  # failure surfaced


def test_classification_is_read_only():
    assert HomelabHealthTool.tool_class is ToolClass.READ_ONLY
