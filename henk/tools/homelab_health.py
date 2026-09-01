"""homelab_health — read-only status over HTTP (Gatus + Prometheus).

Reimplements the intent of the ``homelab-health`` CLI without SSH: the CLI's
Tailscale-SSH approach is admin-privileged and unavailable to Henk by design
(design "Critical finding"). Data comes from the Gatus API (rp5:8080) and the
Prometheus HTTP API (vps:9090) over the tailnet.

Never fabricates: a backend that cannot be reached is reported as an explicit
"source unreachable" line, and the other backend's data is still returned.

**Amended by `read-depth` §10, and this is a user-visible change.** The tool used
to carry three hardcoded constants — memory 90 % used, disk 90 % used, load 8.0 —
none of which was traceable to a rule. Measured against the live alerting systems
(backend probe 1.5) the memory bar that actually delivers reads **75**, the disk
rule is **15 percent free on `/`** rather than 90 percent used, and **no rule
anywhere defines a load bar**. Three memory bars were live simultaneously and this
tool held the one that alerted nobody.

So nothing here declares a number. Both bars are the very :class:`Threshold`
objects ``node_resource_trend`` compares against, taken from the registry by
identity, and both expressions are that query's own expressions widened from one
job to the node-exporter jobs. The spec's "the two tools cannot disagree about a
threshold" scenario then holds by construction rather than by two files agreeing
today. Rollback is a code revert, not a config flip: there is deliberately no
threshold parameter to put back.

Output is projected the same way too — nodes are named by their friendly enum
value, never by the ``instance`` label, and backend free text (which for the
unreachable line means an httpx error quoting the configured base URL) is
scrubbed before it is rendered.
"""

from __future__ import annotations

import logging
from typing import Mapping

import httpx

from henk.tools.base import Tool, ToolClass, ToolResult
from henk.tools.query_registry import (
    QUERY_REGISTRY,
    Threshold,
    compose_gatus_key,
    friendly_target,
    scrub_addresses,
)

logger = logging.getLogger("henk.tools.homelab_health")

_TREND = QUERY_REGISTRY["node_resource_trend"]

#: The three resources this tool summarises, named by their
#: ``node_resource_trend`` resource key. Sharing the key is what makes the
#: threshold lookup below the same object that query renders against; swap and
#: temperature stay out of scope here, so no bar of theirs can conflict.
HEALTH_RESOURCES: tuple[str, ...] = ("memory", "disk", "load")

#: The registry template selects one job; this tool reports the whole fleet, so
#: the selector is widened to the node-exporter jobs. A regex over job names, not
#: an address — the same form D5 uses to derive the DNS node mapping.
FLEET_JOB_SELECTOR = 'job=~"node-exporter.*"'

_JOB_PLACEHOLDER = 'job="<job>"'

#: Short labels for the rendered line. Only cosmetic: every number, unit and bar
#: below comes from the registry.
_LABELS: Mapping[str, str] = {"memory": "mem", "disk": "disk", "load": "load"}


def _fleet_expression(resource: str) -> str:
    """`node_resource_trend`'s expression for one resource, widened by job.

    Derived rather than copied. A second hand-written PromQL string is how the
    measured quantity drifts out of step with the bar it is compared against —
    which is exactly what "disk 90 % used" against a "15 % free" rule was.
    """
    widened = _TREND.expressions[resource].replace(_JOB_PLACEHOLDER, FLEET_JOB_SELECTOR)
    if "<" in widened and ">" in widened:
        raise ValueError(
            f"internal error: unfilled placeholder in the {resource} expression"
        )
    return widened


#: PromQL for the per-node figures, one instant query per resource.
QUERIES: Mapping[str, str] = {
    resource: _fleet_expression(resource) for resource in HEALTH_RESOURCES
}

#: The bars, by identity — not copies of the registry's numbers. ``load`` is
#: absent because no rule in either alerting system defines a bar for it, and an
#: absent key here is what makes its figure render without a verdict.
THRESHOLDS: Mapping[str, Threshold] = {
    resource: _TREND.thresholds[resource]
    for resource in HEALTH_RESOURCES
    if resource in _TREND.thresholds
}

_UNBARRED: tuple[str, ...] = tuple(r for r in HEALTH_RESOURCES if r not in THRESHOLDS)


def _figure(resource: str, value: float) -> str:
    """`mem 52% used` / `disk 55% free` / `load 1.10`.

    The polarity word is taken from the bar's own unit, so a registry that flips
    disk from percent-free to percent-used cannot leave this line describing the
    old quantity.
    """
    label = _LABELS.get(resource, resource)
    bar = THRESHOLDS.get(resource)
    if bar is None:
        return f"{label} {value:.2f}"
    if bar.unit.startswith("percent "):
        return f"{label} {value:.0f}% {bar.unit[len('percent '):]}"
    return f"{label} {value:.2f} {bar.unit}"


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
    ) -> None:
        self._client = client
        self._gatus_url = gatus_url.rstrip("/")
        self._prometheus_url = prometheus_url.rstrip("/")
        self._timeout = timeout

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
            crossings = self._crossings(metrics)
            line = f"{node}: {self._format_metrics(metrics)}"
            if crossings:
                degraded = True
                line += " — DEGRADED: " + "; ".join(crossings)
            lines.append(line)
        if nodes:
            lines.extend(self._bar_notes())
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
            return [], _error_text(exc)
        endpoints: list[tuple[str, bool]] = []
        for entry in data:
            results = entry.get("results") or []
            up = bool(results[-1].get("success")) if results else False
            endpoints.append((_endpoint_name(entry), up))
        return endpoints, None

    async def _fetch_prometheus(self) -> tuple[dict[str, dict[str, float]], str | None]:
        nodes: dict[str, dict[str, float]] = {}
        for resource, query in QUERIES.items():
            try:
                resp = await self._client.get(
                    f"{self._prometheus_url}/api/v1/query",
                    params={"query": query},
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                return nodes, _error_text(exc)
            for sample in payload.get("data", {}).get("result", []):
                # The projection rule: a target is named by its job label, never
                # by `instance` — which is `<tailnet-address>:9100`. A sample with
                # no job cannot be named safely, so it is named as unidentified
                # rather than falling back to the one label that is an address.
                job = sample.get("metric", {}).get("job") or ""
                node = friendly_target(job) if job else "unidentified target"
                value = sample.get("value", [None, None])[1]
                if value is None:
                    continue
                nodes.setdefault(node, {})[resource] = float(value)
        return nodes, None

    @staticmethod
    def _crossings(metrics: Mapping[str, float]) -> list[str]:
        """Which readings sit on the wrong side of their bar.

        ``Threshold.crossed_by`` is the shared predicate — the same call
        ``node_resource_trend``'s renderer makes — so the two tools cannot reach
        opposite verdicts on one reading. A bar flagged ``is_trigger=False`` is
        reported by the query but never raises DEGRADED here: it exists and is
        documented not to be what the rule fires on.
        """
        crossings = []
        for resource in HEALTH_RESOURCES:
            value = metrics.get(resource)
            bar = THRESHOLDS.get(resource)
            if value is None or bar is None or not bar.is_trigger:
                continue
            if bar.crossed_by(value):
                crossings.append(
                    f"{resource} {value:.2f} {bar.unit} crossed the "
                    f"{bar.value:.2f} bar ({bar.source})"
                )
        return crossings

    @staticmethod
    def _bar_notes() -> list[str]:
        """Where the bars come from, and which figures carry no verdict."""
        notes = []
        if THRESHOLDS:
            bars = ", ".join(
                f"{resource} {THRESHOLDS[resource].describe()}"
                for resource in HEALTH_RESOURCES
                if resource in THRESHOLDS
            )
            notes.append(f"Bars, pinned from the live rules: {bars}.")
        if _UNBARRED:
            notes.append(
                f"No bar for {', '.join(_UNBARRED)}: no alert rule in either "
                "alerting system defines one, so the figure is reported without "
                "a verdict."
            )
        return notes

    @staticmethod
    def _format_metrics(metrics: Mapping[str, float]) -> str:
        parts = [
            _figure(resource, metrics[resource])
            for resource in HEALTH_RESOURCES
            if resource in metrics
        ]
        return ", ".join(parts) if parts else "no metrics"


def _endpoint_name(entry: Mapping[str, object]) -> str:
    """Name an endpoint by the key Gatus itself composes.

    The key is what `endpoint_history` takes as an argument, so naming endpoints
    this way lets the owner carry a name straight from one tool to the other. It
    also distinguishes two endpoints sharing a name across groups, which a bare
    `name` does not.
    """
    key = entry.get("key")
    if isinstance(key, str) and key:
        return key
    group, name = entry.get("group"), entry.get("name")
    if isinstance(group, str) and group and isinstance(name, str) and name:
        return compose_gatus_key(group, name)
    if isinstance(name, str) and name:
        return name
    return "unknown"


def _error_text(exc: Exception) -> str:
    """A backend/transport error, with any address it quotes redacted.

    httpx's `HTTPStatusError` quotes the request URL, and the deployed base URLs
    are tailnet addresses — so the "source unreachable" line is an address path
    unless it is scrubbed, the same reason the query renderers scrub Prometheus's
    `lastError`.
    """
    return scrub_addresses(str(exc)) or exc.__class__.__name__
