"""Renderer slots for the named-query registry — filled by task group §4.

Each registry entry points at the renderer named after it, so the mapping is
checkable by inspection and a copy-paste that aims two entries at one renderer
fails a test rather than silently answering the wrong question.

They are **stubs on purpose** at this point in the apply. §3 ships the registry,
the domain validator and the transport boundary; §4 ships the six summaries and
their projection. A stub that raises is the honest intermediate state: the tool
says the renderer is not implemented rather than returning something shaped like
data. The signature is fixed here so §4 fills bodies rather than rewiring slots:

    render_<query_name>(plan: QueryPlan, payloads: Mapping[str, Any]) -> str

``payloads`` is keyed by the plan's expression and route roles, holding each
backend response as parsed JSON. Every renderer MUST run its output through
``query_registry.project_labels`` / ``describe_target`` — results name a target
by its job label and friendly enum value, never by `instance`, `scrapeUrl`,
`globalUrl` or `server` — and MUST carry the entry's ``caveats``, which exist
precisely because a reader cannot infer them from the figures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # pragma: no cover - typing only
    from henk.tools.query_registry import QueryPlan

_PENDING = (
    "its renderer is not implemented yet (read-depth task group 4). The query "
    "was validated and dispatched, but there is nothing here to summarise it."
)


def _pending(name: str) -> str:
    raise NotImplementedError(_PENDING)


def render_unimplemented(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """Default slot: an entry that forgot to name its renderer."""
    return _pending(plan.entry.name)


def render_node_resource_trend(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """§4: first/last/min/max, direction, and the per-resource comparison.

    Never the sample series. `disk` reports percent free against its bar;
    `swap_io` is the primary swap signal; `swap_used` is reported and labelled
    not-the-trigger; `cpu`, `load` and `temperature` carry no bar at all.
    """
    return _pending(plan.entry.name)


def render_scrape_targets(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """§4: every target with its `up` value, plus `lastError` for the down ones.

    Enumerates whatever bare `up` returns — the count is a property of the
    deployed scrape config, not a constant to assert against.
    """
    return _pending(plan.entry.name)


def render_endpoint_history(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """§4: current state, failing condition, transitions, and uptime ratio."""
    return _pending(plan.entry.name)


def render_freshness_check(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """§4: each pipeline's raw timestamp beside the age derived from it."""
    return _pending(plan.entry.name)


def render_container_state(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """§4: per-container last-seen, health, OOM events and creation time."""
    return _pending(plan.entry.name)


def render_dns_performance(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """§4: the derived node mapping, the summary, and the measured baseline."""
    return _pending(plan.entry.name)
