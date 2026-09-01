"""homelab_query — one read-only tool over a closed registry of named queries.

The registry (`henk.tools.query_registry`) holds what may be asked; this module
holds the dispatch. Three properties matter more than the plumbing:

- **Nothing reaches a backend until every argument is a member of a closed set.**
  Validation happens here, in process, not in the JSON schema — model arguments
  are splatted straight into ``_run``, and whether the SDK's MCP layer enforces
  ``input_schema`` carries a standing "verify at deploy" note in this codebase. A
  requirement that tested the schema would test a layer that may enforce nothing.
- **The advertised schema is built FROM the registry.** :func:`build_parameters_schema`
  derives every enum from the same tuples the validator checks against, so a
  domain cannot be advertised that is not enforced. The per-query domains a JSON
  schema cannot express are written into the description, also from the registry.
- **Three outcomes, not two.** In domain and available; out of domain (refused);
  in domain and not derivable (answered, with the specific condition stated).

Registration is task group §7, and the renderers are §4 — an accepted query
currently reaches its backend and then says its summary is not implemented,
rather than returning something shaped like data.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

import httpx

from henk.tools.base import Tool, ToolClass, ToolResult
from henk.tools.query_registry import (
    QUERY_NAMES,
    QUERY_REGISTRY,
    QueryBackend,
    QueryOutcome,
    QueryPlan,
    QueryRefused,
    WINDOW_SECONDS,
    plan_query,
    range_step_seconds,
)

logger = logging.getLogger("henk.tools.homelab_query")


def build_parameters_schema() -> dict[str, Any]:
    """The tool's JSON schema, derived entirely from the registry.

    A JSON schema cannot say "rp2 is valid for this query and not that one", so
    each parameter advertises the union of the domains that use it and the
    description states the per-query split — generated here rather than written
    by hand, because a hand-written one drifts the first time a domain changes.
    """
    properties: dict[str, Any] = {
        "query_name": {
            "type": "string",
            "enum": list(QUERY_NAMES),
            "description": "Which named query to run. "
            + "; ".join(
                f"{name}: {entry.summary}" for name, entry in sorted(QUERY_REGISTRY.items())
            ),
        }
    }
    for name, entry in sorted(QUERY_REGISTRY.items()):
        for parameter in entry.parameters:
            spec = properties.setdefault(
                parameter.name,
                {"type": "string", "description": parameter.description},
            )
            if parameter.domain is None:
                spec.pop("enum", None)
                spec["discovered"] = True
                continue
            if not spec.get("discovered"):
                spec["enum"] = sorted(set(spec.get("enum", [])) | set(parameter.domain))
    # Second pass, once every domain is known: state which query accepts what.
    for parameter_name, spec in properties.items():
        if parameter_name == "query_name":
            continue
        spec.pop("discovered", None)
        per_query = [
            f"{name} accepts {', '.join(p.domain)}"
            if p.domain
            else f"{name}'s values are discovered from the backend at first use"
            for name, entry in sorted(QUERY_REGISTRY.items())
            for p in entry.parameters
            if p.name == parameter_name
        ]
        spec["description"] = spec["description"] + " Per query: " + "; ".join(per_query) + "."
    return {
        "type": "object",
        "properties": properties,
        "required": ["query_name"],
        "additionalProperties": False,
    }


class HomelabQueryTool(Tool):
    name = "homelab_query"
    description = (
        "Answer a homelab question by running one of a fixed set of named, "
        "owner-reviewed queries against Gatus and Prometheus. Read-only. "
        "Accepts no query expression, metric name, label selector or URL: every "
        "argument is chosen from a closed set."
    )
    tool_class = ToolClass.READ_ONLY
    parameters = build_parameters_schema()

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        gatus_url: str,
        prometheus_url: str,
        gatus_timeout: float = 10.0,
        prometheus_timeout: float = 10.0,
        max_points: int = 60,
        discovered_endpoints: tuple[str, ...] | None = None,
        clock=time.time,
    ) -> None:
        self._client = client
        self._gatus_url = gatus_url.rstrip("/")
        self._prometheus_url = prometheus_url.rstrip("/")
        self._gatus_timeout = gatus_timeout
        self._prometheus_timeout = prometheus_timeout
        self._max_points = max_points
        #: §4 replaces this with first-use discovery, memoized with a TTL and a
        #: refresh on lookup miss. ``None`` means discovery has not run, and
        #: every `endpoint_history` call fails closed until it has.
        self._discovered_endpoints = discovered_endpoints
        self._clock = clock

    async def _run(self, **arguments: Any) -> ToolResult:  # type: ignore[override]
        query_name = arguments.pop("query_name", None)
        try:
            plan = plan_query(query_name, arguments, discovered=self._discovered())
        except QueryRefused as refusal:
            return ToolResult.failure(str(refusal))
        if plan.outcome is QueryOutcome.NOT_DERIVABLE:
            return ToolResult.success(plan.message)
        payloads, error = await self._fetch(plan)
        if error is not None:
            return ToolResult.failure(error)
        try:
            return ToolResult.success(plan.entry.renderer(plan, payloads))
        except NotImplementedError as exc:
            return ToolResult.failure(f"{plan.entry.name}: {exc}")

    def _discovered(self) -> dict[str, tuple[str, ...]]:
        if self._discovered_endpoints is None:
            return {}
        return {"endpoint": tuple(self._discovered_endpoints)}

    async def _fetch(
        self, plan: QueryPlan
    ) -> tuple[dict[str, Any], str | None]:
        """Issue the planned requests. Never fabricates on failure."""
        payloads: dict[str, Any] = {}
        for role, expression in plan.expressions.items():
            url, params = self._prometheus_request(plan, expression)
            payload, error = await self._get(
                url, params, self._prometheus_timeout, "Prometheus"
            )
            if error is not None:
                return payloads, error
            payloads[role] = payload
        base = (
            self._gatus_url
            if plan.entry.backend is QueryBackend.GATUS
            else self._prometheus_url
        )
        backend = "Gatus" if plan.entry.backend is QueryBackend.GATUS else "Prometheus"
        timeout = (
            self._gatus_timeout
            if plan.entry.backend is QueryBackend.GATUS
            else self._prometheus_timeout
        )
        for role, route in plan.routes.items():
            payload, error = await self._get(f"{base}{route}", None, timeout, backend)
            if error is not None:
                return payloads, error
            payloads[role] = payload
        return payloads, None

    def _prometheus_request(
        self, plan: QueryPlan, expression: str
    ) -> tuple[str, dict[str, Any]]:
        if not plan.range_query or plan.window is None:
            return f"{self._prometheus_url}/api/v1/query", {"query": expression}
        step = range_step_seconds(plan.window, self._max_points)
        end = self._clock()
        # The step is derived from the window and the configured maximum so that
        # `span / step + 1` — Prometheus's own point count, fencepost included —
        # stays within the budget at every supported window.
        span = WINDOW_SECONDS[plan.window]
        return (
            f"{self._prometheus_url}/api/v1/query_range",
            {"query": expression, "start": end - span, "end": end, "step": step},
        )

    async def _get(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        timeout: float,
        backend: str,
    ) -> tuple[Any, str | None]:
        try:
            response = await self._client.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json(), None
        except httpx.TimeoutException:
            return None, f"{backend} timed out after {timeout:.0f}s"
        except httpx.HTTPStatusError as exc:
            return None, f"{backend} returned HTTP {exc.response.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            return None, f"{backend} request failed: {exc}"
