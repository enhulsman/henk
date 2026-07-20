"""homelab_health — read-only status over HTTP (Gatus + Prometheus).

Reimplements the intent of the ``homelab-health`` CLI without SSH: the CLI's
Tailscale-SSH approach is admin-privileged and unavailable to Henk by design
(design "Critical finding"). Data comes from the Gatus API (rp5:8080) and the
Prometheus HTTP API (vps:9090) over the tailnet.

Never fabricates: a backend that cannot be reached is reported as an explicit
"source unreachable" line, and the other backend's data is still returned.
"""

from __future__ import annotations

import logging

import httpx

from henk.tools.base import Tool, ToolClass, ToolResult

logger = logging.getLogger("henk.tools.homelab_health")

# PromQL for the per-node figures we summarise. Each yields one sample per node.
_QUERIES = {
    "memory_used_pct": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
    "disk_used_pct": '100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})',
    "load1": "node_load1",
}


class HomelabHealthTool(Tool):
    name = "homelab_health"
    description = (
        "Report homelab health: endpoint up/down (Gatus) and per-node memory, "
        "disk, and load (Prometheus). Read-only, no arguments."
    )
    tool_class = ToolClass.READ_ONLY
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        gatus_url: str,
        prometheus_url: str,
        timeout: float = 10.0,
        memory_threshold_pct: float = 90.0,
        disk_threshold_pct: float = 90.0,
        load_threshold: float = 8.0,
    ) -> None:
        self._client = client
        self._gatus_url = gatus_url.rstrip("/")
        self._prometheus_url = prometheus_url.rstrip("/")
        self._timeout = timeout
        self._mem_threshold = memory_threshold_pct
        self._disk_threshold = disk_threshold_pct
        self._load_threshold = load_threshold

    async def _run(self) -> ToolResult:  # type: ignore[override]
        lines: list[str] = []
        degraded = False

        endpoints, gatus_err = await self._fetch_gatus()
        if gatus_err is not None:
            lines.append(f"Gatus: source unreachable ({gatus_err})")
        elif not endpoints:
            lines.append("Gatus: reachable but reported no endpoints")
        else:
            down = [name for name, up in endpoints if not up]
            if down:
                degraded = True
                lines.append(
                    f"Gatus: {len(endpoints)} endpoints, DOWN: {', '.join(down)}"
                )
            else:
                lines.append(f"Gatus: all {len(endpoints)} endpoints healthy")

        nodes, prom_err = await self._fetch_prometheus()
        # Render whatever node data we collected, even if a later query failed —
        # partial real data beats discarding it. Any failure is surfaced too.
        for node, metrics in sorted(nodes.items()):
            problems = self._node_problems(metrics)
            if problems:
                degraded = True
                lines.append(f"{node}: DEGRADED — {'; '.join(problems)}")
            else:
                lines.append(f"{node}: {self._format_metrics(metrics)}")
        if prom_err is not None:
            lines.append(f"Prometheus: source unreachable ({prom_err})")

        status = "DEGRADED" if degraded else "healthy"
        summary = f"Homelab status: {status}\n" + "\n".join(lines)
        return ToolResult.success(summary)

    async def _fetch_gatus(self) -> tuple[list[tuple[str, bool]], str | None]:
        try:
            resp = await self._client.get(
                f"{self._gatus_url}/api/v1/endpoints/statuses",
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return [], str(exc) or exc.__class__.__name__
        endpoints: list[tuple[str, bool]] = []
        for entry in data:
            name = entry.get("name") or entry.get("key") or "unknown"
            results = entry.get("results") or []
            up = bool(results[-1].get("success")) if results else False
            endpoints.append((name, up))
        return endpoints, None

    async def _fetch_prometheus(self) -> tuple[dict[str, dict[str, float]], str | None]:
        nodes: dict[str, dict[str, float]] = {}
        for metric, query in _QUERIES.items():
            try:
                resp = await self._client.get(
                    f"{self._prometheus_url}/api/v1/query",
                    params={"query": query},
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                return nodes, str(exc) or exc.__class__.__name__
            for sample in payload.get("data", {}).get("result", []):
                instance = sample.get("metric", {}).get("instance", "unknown")
                value = sample.get("value", [None, None])[1]
                if value is None:
                    continue
                nodes.setdefault(instance, {})[metric] = float(value)
        return nodes, None

    def _node_problems(self, metrics: dict[str, float]) -> list[str]:
        problems = []
        mem = metrics.get("memory_used_pct")
        if mem is not None and mem > self._mem_threshold:
            problems.append(f"memory {mem:.0f}% > {self._mem_threshold:.0f}%")
        disk = metrics.get("disk_used_pct")
        if disk is not None and disk > self._disk_threshold:
            problems.append(f"disk {disk:.0f}% > {self._disk_threshold:.0f}%")
        load = metrics.get("load1")
        if load is not None and load > self._load_threshold:
            problems.append(f"load {load:.2f} > {self._load_threshold:.2f}")
        return problems

    @staticmethod
    def _format_metrics(metrics: dict[str, float]) -> str:
        parts = []
        if "memory_used_pct" in metrics:
            parts.append(f"mem {metrics['memory_used_pct']:.0f}%")
        if "disk_used_pct" in metrics:
            parts.append(f"disk {metrics['disk_used_pct']:.0f}%")
        if "load1" in metrics:
            parts.append(f"load {metrics['load1']:.2f}")
        return ", ".join(parts) if parts else "no metrics"
