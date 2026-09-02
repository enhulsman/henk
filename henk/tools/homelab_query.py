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

from henk.tools.backend_failure import backend_failure_reason
from henk.tools.base import Tool, ToolClass, ToolResult
from henk.tools.query_registry import (
    QUERY_NAMES,
    QUERY_REGISTRY,
    QueryBackend,
    QueryEntry,
    QueryOutcome,
    QueryPlan,
    QueryRefused,
    WINDOW_SECONDS,
    plan_query,
    range_step_seconds,
    resolve_endpoint_key,
)

#: Where the Gatus endpoint key set is discovered from. The bulk statuses route
#: is the only one that enumerates keys.
ENDPOINT_DISCOVERY_ROUTE = "/api/v1/endpoints/statuses"

#: How long a discovered key set is trusted before it is re-read. Not a config
#: key: the deployed `config.yaml` is skip-worktree'd and cannot carry new keys,
#: and a lookup miss refreshes regardless of the TTL — so the interval only
#: bounds how long a *deleted* endpoint stays queryable, never how long a *new*
#: one stays invisible.
DEFAULT_DISCOVERY_TTL_SECONDS = 300.0

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
        endpoint_discovery_ttl: float = DEFAULT_DISCOVERY_TTL_SECONDS,
        clock=time.time,
    ) -> None:
        self._client = client
        self._gatus_url = gatus_url.rstrip("/")
        self._prometheus_url = prometheus_url.rstrip("/")
        self._gatus_timeout = gatus_timeout
        self._prometheus_timeout = prometheus_timeout
        self._max_points = max_points
        #: The discovered Gatus key set, memoized. Seeding it here is a test and
        #: startup seam only — nothing fetches during construction, which
        #: `build_runtime`'s "nothing network-facing is opened here" contract
        #: requires. ``None`` means discovery has not run yet.
        self._discovered_endpoints = (
            tuple(discovered_endpoints) if discovered_endpoints is not None else None
        )
        self._discovery_ttl = endpoint_discovery_ttl
        self._clock = clock
        self._discovered_at = clock() if discovered_endpoints is not None else None

    async def _run(self, **arguments: Any) -> ToolResult:  # type: ignore[override]
        query_name = arguments.pop("query_name", None)
        entry = QUERY_REGISTRY.get(query_name) if isinstance(query_name, str) else None

        discovered: dict[str, tuple[str, ...]] = {}
        if entry is not None and any(p.discovered for p in entry.parameters):
            keys, error = await self._resolve_discovered(entry, arguments)
            if error is not None:
                return ToolResult.failure(error)
            discovered["endpoint"] = keys or ()

        try:
            plan = plan_query(query_name, arguments, discovered=discovered)
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
        except Exception:  # pragma: no cover - defensive
            # A shape nobody anticipated must not crash the turn, and must not
            # produce a partial result presented as a whole one.
            logger.exception("rendering %s failed", plan.entry.name)
            return ToolResult.failure(
                f"{plan.entry.name}: the backend answered, but its response could "
                "not be summarised. No partial result is returned."
            )

    # --- Discovered domains -------------------------------------------------

    async def _resolve_discovered(
        self, entry: QueryEntry, arguments: dict[str, Any]
    ) -> tuple[tuple[str, ...] | None, str | None]:
        """Discover the Gatus key set and resolve the supplied value into it.

        Two things happen here and both are part of the closed-set boundary. The
        key set is **discovered at first use** rather than at construction, so a
        rename picks up without a restart and a discovery outage fails one
        invocation instead of permanently disabling the query. And the supplied
        value is **resolved against that set** — an arriving Gatus event names
        `{group}/{endpoint}` while the queryable key is the composed, sanitized
        form, so without this step the agent would be guessing at a key.

        A miss refreshes once. Anything that still does not resolve is left as
        supplied, so `plan_query` refuses it by name: this function never invents
        a key and never widens the set.
        """
        keys, error = await self._endpoint_keys()
        if error is not None:
            return None, error
        supplied = arguments.get("endpoint")
        if not isinstance(supplied, str) or supplied in (keys or ()):
            return keys, None
        resolved = resolve_endpoint_key(supplied, keys or ())
        if resolved is None:
            keys, error = await self._endpoint_keys(refresh=True)
            if error is not None:
                return None, error
            resolved = resolve_endpoint_key(supplied, keys or ())
        if resolved is not None:
            arguments["endpoint"] = resolved
        return keys, None

    async def _endpoint_keys(
        self, *, refresh: bool = False
    ) -> tuple[tuple[str, ...] | None, str | None]:
        fresh = (
            self._discovered_endpoints is not None
            and self._discovered_at is not None
            and (self._clock() - self._discovered_at) < self._discovery_ttl
        )
        if fresh and not refresh:
            return self._discovered_endpoints, None
        payload, error = await self._get(
            f"{self._gatus_url}{ENDPOINT_DISCOVERY_ROUTE}",
            None,
            self._gatus_timeout,
            "Gatus",
        )
        if error is not None:
            # Fail closed, always: a discovery outage must refuse the invocation
            # rather than fall back to a stale set or to treating the argument as
            # free text.
            return None, (
                f"Gatus endpoint discovery failed, so no endpoint key can be "
                f"validated and none is passed through: {error}"
            )
        if not isinstance(payload, list):
            return None, (
                "Gatus endpoint discovery returned an unexpected shape, so no "
                "endpoint key can be validated and none is passed through."
            )
        keys = tuple(
            entry["key"]
            for entry in payload
            if isinstance(entry, Mapping) and isinstance(entry.get("key"), str)
        )
        self._discovered_endpoints = keys
        self._discovered_at = self._clock()
        return keys, None

    async def _fetch(
        self, plan: QueryPlan
    ) -> tuple[dict[str, Any], str | None]:
        """Issue the planned requests. Never fabricates on failure."""
        payloads: dict[str, Any] = {}
        for role, expression in plan.expressions.items():
            url, params = self._prometheus_request(plan, expression, role)
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
        self, plan: QueryPlan, expression: str, role: str = ""
    ) -> tuple[str, dict[str, Any]]:
        roles = plan.entry.range_roles
        is_range = plan.range_query and (roles is None or role in roles)
        if not is_range or plan.window is None:
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
        except (httpx.HTTPError, ValueError) as exc:
            # The three sentences live in `backend_failure_reason` so that
            # `sessions_read` cannot drift a word away from them.
            return None, backend_failure_reason(backend, exc, timeout=timeout)
