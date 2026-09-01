"""The named-query registry: reviewable data, not scattered strings.

`homelab_query` answers homelab questions by dispatching a **closed enum** of
owner-reviewed entries. This module is that enum, its parameter domains, its
templates, and the validator that stands between a model argument and an HTTP
request. Everything a reviewer needs to judge what this tool can reach is in one
table here.

Three properties hold this module together, and each exists because its absence
has a named failure mode:

**Every value in it is measured.** Job names, metric names, window vocabularies,
thresholds and baselines all come from ``openspec/changes/read-depth/notes/
backend-probe.md``, a live probe of the deployed Gatus, Prometheus and Grafana.
None is transcribed from documentation prose: this change caught three
transcribe-from-prose defects in review, two of them inside fixes written to
prevent exactly that, and the docs' DNS baselines were measured to be an order of
magnitude wrong on two of three devices. A test compares every threshold in here
against that record and fails on a value the record does not pin.

**Templates select by ``job`` label, never by address.** Prometheus ``instance``
labels are ``<tailnet-address>:9100``, ``/api/v1/targets`` carries ``scrapeUrl``
and ``globalUrl``, and the AdGuard metrics' only discriminator (``server``) is an
``http://<addr>:<port>`` URL. Job names carry no address, so the registry can be
committed to a publication-bound repo and rp5's ``config.yaml`` needs no address
either. Results are **projected** through :func:`project_labels` for the same
reason.

**Domains are per-query, and they are the same object the schema advertises.**
The fleet is not uniform — ``node_resource_trend`` accepts ``rp2`` and
``container_state`` does not, because rp2 runs no cadvisor. A single global node
enum would accept ``container_state(node=rp2)`` and return an empty list, which
reads as "no containers" rather than "not measured here". :func:`domain_for`
returns the tuple the validator itself checks against, so a domain cannot be
advertised that is not enforced.

There is deliberately **no free-text path**: no parameter accepts a PromQL
expression, a metric name, a label selector, a URL or a filesystem path, and no
configuration key, debug flag, or code path admits one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping
from urllib.parse import quote

from henk.tools import query_renderers as renderers

# --- Measured constants (backend-probe.md) --------------------------------

#: Node enum value -> node-exporter job label (record 1.3). The registry stores
#: job names because they carry no address; `instance` labels do.
NODE_EXPORTER_JOBS: Mapping[str, str] = {
    "rp5": "node-exporter-pi5",
    "vps": "node-exporter-vps",
    "rp2": "node-exporter-pi2",
}

#: Node enum value -> cadvisor job label. **rp2 is absent on purpose**: it runs
#: no cadvisor, which is why `container_state`'s node domain is narrower.
CADVISOR_JOBS: Mapping[str, str] = {
    "rp5": "cadvisor-pi5",
    "vps": "cadvisor-vps",
}

#: The reverse map, so a result can name a target by its friendly enum value.
#: Jobs with no node of their own (`adguard-exporter`, `pushgateway`) are absent
#: and are named by their job label instead.
NODE_FOR_JOB: Mapping[str, str] = {
    **{job: node for node, job in NODE_EXPORTER_JOBS.items()},
    **{job: node for node, job in CADVISOR_JOBS.items()},
}

#: Prometheus's windows. All four sit inside the measured 15-day retention.
PROMETHEUS_WINDOWS: tuple[str, ...] = ("15m", "1h", "6h", "24h")

#: Gatus's windows — the deployed server's OWN vocabulary, read from its 400
#: body (`Durations supported: 30d, 7d, 24h, 1h`). It rejects `15m` and `6h`, so
#: this is deliberately NOT `PROMETHEUS_WINDOWS`; they overlap on {1h, 24h}.
#: `30d` exceeds Prometheus retention but is served from Gatus's own history.
GATUS_WINDOWS: tuple[str, ...] = ("1h", "24h", "7d", "30d")

#: The seven resources `node_resource_trend` measures, in the spec's order.
RESOURCES: tuple[str, ...] = (
    "cpu",
    "memory",
    "disk",
    "load",
    "swap_used",
    "swap_io",
    "temperature",
)

WINDOW_SECONDS: Mapping[str, int] = {
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}

#: `scrape_targets`' lookback. A literal, not a parameter: the question is "is
#: anything down and since when", and one window answers it.
SCRAPE_TARGETS_LOOKBACK = "24h"

#: The metric all fourteen live DNS rules evaluate (7 native + 7 Grafana), so it
#: is the one that satisfies "measure the rule's input". NOT
#: `adguard_top_upstreams_avg_response_time_seconds`, which carries a fourth
#: address-bearing label.
DNS_METRIC = "adguard_avg_processing_time_seconds"

#: The eight raw-timestamp gauges, enumerated from the live
#: `/api/v1/label/__name__/values` (record 1.2). Note the naming is NOT uniform:
#: `health_etl_*` carries a `_seconds` suffix and every other family does not, so
#: a `*_timestamp` glob would silently drop the health-ETL metric. The selector
#: below is built from this tuple, so the two cannot drift.
FRESHNESS_TIMESTAMP_METRICS: tuple[str, ...] = (
    "homelab_backup_last_run_timestamp",
    "homelab_backup_last_success_timestamp",
    "homelab_backup_rotate_last_run_timestamp",
    "homelab_dump_mtime_timestamp",
    "health_etl_last_success_timestamp_seconds",
    "obsidian_backup_push_last_run_timestamp",
    "obsidian_backup_push_last_success_timestamp",
    "obsidian_backup_verify_last_run_timestamp",
)

#: Labels and fields that carry an address and must never be rendered. Six of the
#: seven were measured on live responses; `upstream` comes from the AdGuard
#: metric this registry deliberately does not use. `server` is listed because it
#: LOOKS like a safe discriminator and is not: it is a URL containing a tailnet
#: address.
ADDRESS_BEARING_LABELS: frozenset[str] = frozenset(
    {
        "instance",
        "server",
        "scrapeUrl",
        "globalUrl",
        "__address__",
        "hostname",
        "upstream",
    }
)

_ADDRESS_BEARING_LOWER = frozenset(label.lower() for label in ADDRESS_BEARING_LABELS)


def _is_address_shaped(value: str) -> bool:
    """A value that looks like an address, whatever label it arrives under.

    The name-based denial above only catches labels someone enumerated; a new
    exporter label is exactly how the next address would reach a result.
    """
    if "://" in value:
        return True
    parts = value.split(":", 1)[0].split(".")
    return len(parts) == 4 and all(part.isdigit() for part in parts)


class QueryBackend(str, Enum):
    """Which service an entry reads. Both are already inside `tag:henk`."""

    PROMETHEUS = "prometheus"
    GATUS = "gatus"


class QueryOutcome(str, Enum):
    """The three distinguishable outcomes of an invocation.

    Two would not be enough. A value can be inside its declared domain and still
    have no data behind it — `temperature` on the vps, whose node-exporter
    exposes neither thermal metric — and that is neither a refusal nor a
    measurement that came back empty. Collapsing it into either one tells the
    owner something false.
    """

    ANSWERED = "answered"
    OUT_OF_DOMAIN = "out-of-domain"
    NOT_DERIVABLE = "not-derivable"


class QueryRefused(ValueError):
    """An argument that never becomes a request. Carries its own outcome."""

    def __init__(self, message: str, outcome: QueryOutcome = QueryOutcome.OUT_OF_DOMAIN):
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class Threshold:
    """One bar, traceable to the live rule expression that defines it.

    ``is_trigger`` false means the bar exists but is **not** what the rule fires
    on — `swap_used` is the case that earns the flag, because swap fullness and
    swap pressure are anti-correlated on this fleet and presenting a fullness
    figure as "approaching the bar" would be an alarm about a documented
    non-condition.
    """

    value: float
    unit: str
    direction: str  # "above" | "below" — the rule's own form
    source: str  # the live rule whose expression pins this number
    is_trigger: bool = True
    note: str = ""


@dataclass(frozen=True)
class QueryParameter:
    """One bound parameter and the closed set it accepts.

    ``domain`` of ``None`` means the set is **discovered** from the backend at
    first use rather than hardcoded here (Gatus endpoint keys). Discovered is not
    open: an argument absent from the discovered set is refused, and a discovery
    that has not run refuses everything.
    """

    name: str
    domain: tuple[str, ...] | None
    description: str
    note: str = ""
    discovery_route: str | None = None

    @property
    def discovered(self) -> bool:
        return self.domain is None


@dataclass(frozen=True)
class Unavailable:
    """A parameter combination that is in domain and has no data behind it.

    Measured, never assumed — each one cites the probe record. ``aspect`` names a
    single missing field when the rest of the query still answers
    (`container_health_state` on rp5); ``None`` means the whole query has nothing
    to read (`temperature` on the vps).
    """

    parameters: Mapping[str, str]
    aspect: str | None
    reason: str


@dataclass(frozen=True)
class QueryEntry:
    """One reviewable row of the registry."""

    name: str
    backend: QueryBackend
    summary: str
    #: PromQL expressions, keyed by role — or, for `node_resource_trend`, by the
    #: parameter value that selects them (see ``select_by``).
    expressions: Mapping[str, str] = field(default_factory=dict)
    #: HTTP API routes, keyed by role. Placeholders are `<name>` tokens.
    routes: Mapping[str, str] = field(default_factory=dict)
    parameters: tuple[QueryParameter, ...] = ()
    renderer: Callable[..., str] = renderers.render_unimplemented
    thresholds: Mapping[str, Threshold] = field(default_factory=dict)
    baselines: Mapping[str, float] = field(default_factory=dict)
    #: True for the two entries that issue Prometheus **range** queries and
    #: return a bounded summary rather than the sample series.
    range_query: bool = False
    #: The parameter whose value selects one expression from ``expressions``.
    select_by: str | None = None
    #: Enum value -> job label for this entry's `node` parameter.
    job_map: Mapping[str, str] | None = None
    #: Expression roles expected to match exactly one series. More than one is
    #: reported as such rather than rendered as the target's figure.
    single_series: frozenset[str] = frozenset()
    #: Statements the result must carry — what the query cannot see, and what it
    #: silently omits. They travel with the entry so a renderer cannot forget one.
    caveats: tuple[str, ...] = ()
    unavailable: tuple[Unavailable, ...] = ()
    lookback: str | None = None
    #: `dns_performance` only: the node↔series mapping is derived at query time
    #: from job-labelled series, never stored here or in configuration.
    derives_node_mapping: bool = False
    #: The form a template naming one container must take, so an absent series
    #: yields a reading rather than no series at all. cadvisor drops a container's
    #: series entirely when it stops, which is how `MollySocketLiveness` works.
    named_container_template: str = ""

    @property
    def templates(self) -> dict[str, str]:
        """Every template this entry can issue, expressions and routes alike."""
        return {**self.expressions, **self.routes}

    def parameter(self, name: str) -> QueryParameter:
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        raise KeyError(f"{self.name} declares no parameter {name!r}")


@dataclass(frozen=True)
class QueryPlan:
    """A validated invocation: what will be requested, and nothing more."""

    entry: QueryEntry
    arguments: Mapping[str, str]
    outcome: QueryOutcome
    expressions: Mapping[str, str] = field(default_factory=dict)
    routes: Mapping[str, str] = field(default_factory=dict)
    caveats: tuple[str, ...] = ()
    message: str = ""
    window: str | None = None

    @property
    def range_query(self) -> bool:
        return self.entry.range_query


# --- The six entries -------------------------------------------------------

_NODE_PARAM_DESCRIPTION = "Which node to measure."
_WINDOW_PROM_DESCRIPTION = "How far back to look."

_FRESHNESS_SELECTOR = (
    '{__name__=~"' + "|".join(FRESHNESS_TIMESTAMP_METRICS) + '"}'
)

_NODE_RESOURCE_EXPRESSIONS: Mapping[str, str] = {
    # Percent busy, from the idle counter — the native rule's own form, but
    # aggregated `by (job)` rather than `by (instance)` so no address is
    # rendered into the result.
    "cpu": '100 - avg by (job) (rate(node_cpu_seconds_total{job="<job>",mode="idle"}[5m])) * 100',
    # Percent USED, matching the Grafana rule `(1 - avail/total) * 100 > 75`.
    "memory": '100 * (1 - node_memory_MemAvailable_bytes{job="<job>"} / node_memory_MemTotal_bytes{job="<job>"})',
    # Percent FREE on `/` only, matching `HenkDiskPressure` exactly. Not "85%
    # used", and not across all filesystems.
    "disk": '100 * node_filesystem_avail_bytes{job="<job>",mountpoint="/"} / node_filesystem_size_bytes{job="<job>",mountpoint="/"}',
    "load": 'node_load1{job="<job>"}',
    # Percent full — `HenkSwapPressure`'s first term, which is NOT what it
    # practically fires on.
    "swap_used": '100 * (1 - node_memory_SwapFree_bytes{job="<job>"} / node_memory_SwapTotal_bytes{job="<job>"})',
    # Pages/s — `HenkSwapPressure`'s second term, the primary swap signal.
    "swap_io": 'rate(node_vmstat_pswpin{job="<job>"}[5m]) + rate(node_vmstat_pswpout{job="<job>"}[5m])',
    # One sensor, named. `node_hwmon_temp_celsius` spans several chips including
    # an NVMe sensor on rp5, so a bare "temperature" over it would report a disk
    # sensor as the CPU.
    "temperature": 'node_thermal_zone_temp{job="<job>",type="cpu-thermal"}',
}

_CONTAINER_EXPRESSIONS: Mapping[str, str] = {
    "last_seen": 'container_last_seen{job="<job>",name!=""}',
    "health_state": 'container_health_state{job="<job>",name!=""}',
    "oom_events": 'container_oom_events_total{job="<job>",name!=""}',
    # Labelled "created" everywhere it surfaces: this value does not move across
    # a `docker restart` or a crash loop, which is why `HenkContainerRestarting`
    # is deployed and structurally cannot fire.
    "created": 'container_start_time_seconds{job="<job>",name!=""}',
}

_REGISTRY: dict[str, QueryEntry] = {
    "node_resource_trend": QueryEntry(
        name="node_resource_trend",
        backend=QueryBackend.PROMETHEUS,
        summary="Is one node's resource climbing, flat, or recovering?",
        expressions=_NODE_RESOURCE_EXPRESSIONS,
        parameters=(
            QueryParameter("node", tuple(NODE_EXPORTER_JOBS), _NODE_PARAM_DESCRIPTION),
            QueryParameter("resource", RESOURCES, "Which measurement to trend."),
            QueryParameter("window", PROMETHEUS_WINDOWS, _WINDOW_PROM_DESCRIPTION),
        ),
        renderer=renderers.render_node_resource_trend,
        thresholds={
            "disk": Threshold(
                15.0,
                "percent free",
                "below",
                "HenkDiskPressure",
                note="scoped to mountpoint=/ and reported as percent free, the "
                "rule's own form — not percent used, and not across all "
                "filesystems.",
            ),
            "swap_io": Threshold(
                50.0,
                "pages/s",
                "above",
                "HenkSwapPressure",
                note="the primary swap signal: this is what the rule actually "
                "fires on.",
            ),
            "swap_used": Threshold(
                95.0,
                "percent full",
                "above",
                "HenkSwapPressure",
                is_trigger=False,
                note="NOT the rule's trigger. Fullness and pressure are "
                "anti-correlated on this fleet — the vps sits chronically at "
                "64-90% full while a Pi at 6% fullness hit 128 pages/s — so a "
                "fullness figure below this bar is normal, not an approaching "
                "incident.",
            ),
            "memory": Threshold(
                75.0,
                "percent used",
                "above",
                "High memory usage",
                note="delivers to Discord, not to henk-events. Three memory bars "
                "are live at once (Grafana 75, Prometheus-native 90, "
                "homelab_health's constant 90); 75 is the one that alerts.",
            ),
        },
        range_query=True,
        select_by="resource",
        job_map=NODE_EXPORTER_JOBS,
        single_series=frozenset({"cpu", "memory", "disk", "load", "swap_used", "swap_io", "temperature"}),
        caveats=(
            "cpu, load and temperature carry no threshold: no rule defines one "
            "in either alerting system.",
            "temperature has no alert in either alerting system — rp2 has no "
            "active cooling and nothing watches it.",
        ),
        unavailable=(
            Unavailable(
                parameters={"node": "vps", "resource": "temperature"},
                aspect=None,
                reason="the vps node-exporter exposes neither "
                "node_thermal_zone_temp nor node_hwmon_temp_celsius, so there is "
                "no temperature series to trend (measured, backend probe 1.4).",
            ),
        ),
    ),
    "scrape_targets": QueryEntry(
        name="scrape_targets",
        backend=QueryBackend.PROMETHEUS,
        summary="Which exporters are down, since when, and why?",
        expressions={
            # BARE `up`, never `up == 0`: a filtered query returns an empty set
            # when the fleet is healthy, which is indistinguishable from a broken
            # or misdirected query. Enumerating every target proves it ran.
            "up": "up",
            # Was the target up at any point in the window? 0 means never, which
            # bounds "down since" honestly instead of inventing a duration.
            "up_over_window": f"max_over_time(up[{SCRAPE_TARGETS_LOOKBACK}])",
        },
        # `lastError` lives on the targets API, not in a metric. The documented
        # answer to "why is it down" is almost always a Tailscale grant.
        routes={"targets": "/api/v1/targets"},
        renderer=renderers.render_scrape_targets,
        lookback=SCRAPE_TARGETS_LOOKBACK,
        caveats=(
            "A target that was never up inside the window is reported as down "
            "for longer than the window, with no specific duration.",
            "scrapeUrl, globalUrl, instance and __address__ all carry addresses "
            "and are dropped; targets are named by job.",
        ),
    ),
    "endpoint_history": QueryEntry(
        name="endpoint_history",
        backend=QueryBackend.GATUS,
        summary="One endpoint's failures, uptime, latency and failing condition.",
        routes={
            "statuses": "/api/v1/endpoints/<endpoint>/statuses",
            "uptime": "/api/v1/endpoints/<endpoint>/uptimes/<window>",
        },
        parameters=(
            QueryParameter(
                "endpoint",
                None,
                "Which Gatus endpoint, by its key. The key set is discovered "
                "from Gatus at first use, so a renamed endpoint becomes "
                "queryable without a restart.",
                discovery_route="/api/v1/endpoints/statuses",
            ),
            QueryParameter(
                "window",
                GATUS_WINDOWS,
                "Uptime window. Gatus's own vocabulary, which is narrower than "
                "Prometheus's and shares only 1h and 24h with it.",
            ),
        ),
        renderer=renderers.render_endpoint_history,
        caveats=(
            "Gatus stores only about 1.7 hours of individual check results, so "
            "'since when' comes from the transition events, not the results.",
            "Gatus history resets on a config change and is excluded from "
            "backup, so an empty history is routine rather than a fault.",
        ),
    ),
    "freshness_check": QueryEntry(
        name="freshness_check",
        backend=QueryBackend.PROMETHEUS,
        summary="Backup, dump and ETL pipeline ages, from their raw timestamps.",
        expressions={"timestamps": _FRESHNESS_SELECTOR},
        renderer=renderers.render_freshness_check,
        caveats=(
            "Each pipeline's RAW timestamp is reported beside the derived age: a "
            "writer that dies freezes a raw timestamp so the age keeps growing, "
            "whereas a precomputed age would freeze and hide the failure.",
        ),
    ),
    "container_state": QueryEntry(
        name="container_state",
        backend=QueryBackend.PROMETHEUS,
        summary="Per-container last-seen, health, OOM events and creation time.",
        expressions=_CONTAINER_EXPRESSIONS,
        parameters=(
            QueryParameter(
                "node",
                tuple(CADVISOR_JOBS),
                _NODE_PARAM_DESCRIPTION,
                note="rp2 runs no cadvisor, so it is outside this query's "
                "domain rather than an empty container list",
            ),
        ),
        renderer=renderers.render_container_state,
        job_map=CADVISOR_JOBS,
        named_container_template='max(container_last_seen{job="<job>",name="<container>"}) or vector(0)',
        caveats=(
            "container_start_time_seconds is the container's CREATION time, not "
            "its last start: it does not move across a restart or a crash loop, "
            "so in-place restart loops are not observable from these metrics.",
            "A container absent from this list may be stopped or removed rather "
            "than absent from the host — the exporter drops a container's series "
            "entirely when it stops.",
        ),
        unavailable=(
            Unavailable(
                parameters={"node": "rp5"},
                aspect="health_state",
                reason="container_health_state has no series on cadvisor-pi5 "
                "(measured, backend probe 1.4). Last-seen, OOM events and "
                "creation time are unaffected; the health column is unavailable "
                "on this node rather than empty, and an omitted health column "
                "would read as 'no container is unhealthy'.",
            ),
        ),
    ),
    "dns_performance": QueryEntry(
        name="dns_performance",
        backend=QueryBackend.PROMETHEUS,
        summary="AdGuard resolution latency against its measured baseline.",
        expressions={
            # Unselected on purpose: the metric's only discriminator is `server`,
            # an http://<addr>:<port> URL. Selecting on it would put three
            # tailnet addresses in this repo.
            "series": DNS_METRIC,
            # The mapping is DERIVED from this — a selector naming only jobs —
            # by matching hosts in memory. Verified 1:1 live, twice.
            "node_mapping": 'up{job=~"node-exporter.*"}',
        },
        parameters=(
            QueryParameter("node", tuple(NODE_EXPORTER_JOBS), _NODE_PARAM_DESCRIPTION),
            QueryParameter("window", PROMETHEUS_WINDOWS, _WINDOW_PROM_DESCRIPTION),
        ),
        renderer=renderers.render_dns_performance,
        thresholds={
            "rp5_warning": Threshold(60.0, "ms", "above", "Pi5HighDNSProcessingTime"),
            "rp5_critical": Threshold(150.0, "ms", "above", "Pi5CriticalDNSProcessingTime"),
            "vps_warning": Threshold(80.0, "ms", "above", "VPSHighDNSProcessingTime"),
            "vps_critical": Threshold(200.0, "ms", "above", "VPSCriticalDNSProcessingTime"),
            "rp2_warning": Threshold(200.0, "ms", "above", "Pi2HighDNSProcessingTime"),
            "rp2_critical": Threshold(500.0, "ms", "above", "Pi2CriticalDNSProcessingTime"),
            "fleet_critical": Threshold(
                300.0,
                "ms",
                "above",
                "DNSProcessingTimeCritical",
                note="fleet-wide rule, unselected: it applies to every device.",
            ),
        },
        # 24h averages in milliseconds, measured 2026-09-01. The documented
        # baselines (17/41/134 ms) are six months stale and wrong by an order of
        # magnitude on two of three devices, with the ordering inverted.
        baselines={"rp5": 2.40, "vps": 2.74, "rp2": 74.72},
        range_query=True,
        derives_node_mapping=True,
        caveats=(
            "This metric is AdGuard's own rolling average over its internal "
            "stats period, not an instantaneous latency: over 15m or 1h it "
            "barely moves, so a flat summary is the metric's nature rather than "
            "a broken query.",
        ),
    ),
}

QUERY_REGISTRY: Mapping[str, QueryEntry] = _REGISTRY

#: What the tool advertises, derived from the registry so the enum and the
#: dispatch table are one object.
QUERY_NAMES: tuple[str, ...] = tuple(sorted(QUERY_REGISTRY))


# --- Domains, validation, planning ----------------------------------------


def domain_for(query_name: str, parameter: str) -> tuple[str, ...] | None:
    """THE domain — the same tuple the validator checks against.

    Returning the object rather than a copy is the point: an advertised domain
    and an enforced domain that are merely *equal* can be edited apart.
    """
    return QUERY_REGISTRY[query_name].parameter(parameter).domain


def range_step_seconds(window: str, max_points: int) -> int:
    """Step that keeps a window inside its configured point budget."""
    if max_points < 2:
        raise ValueError("a range summary needs at least two points")
    span = WINDOW_SECONDS[window]
    return max(1, math.ceil(span / (max_points - 1)))


def expected_point_count(window: str, max_points: int) -> int:
    span = WINDOW_SECONDS[window]
    return span // range_step_seconds(window, max_points) + 1


def _fill(template: str, values: Mapping[str, str], *, encode: bool) -> str:
    """Substitute `<name>` placeholders; refuse to emit an unfilled one."""
    filled = template
    for name, value in values.items():
        replacement = quote(value, safe="") if encode else value
        filled = filled.replace(f"<{name}>", replacement)
    if "<" in filled and ">" in filled:
        raise QueryRefused(
            f"internal error: unfilled placeholder in {template!r}; no request "
            "was issued"
        )
    return filled


def plan_query(
    query_name: str | None,
    arguments: Mapping[str, Any] | None = None,
    *,
    discovered: Mapping[str, tuple[str, ...]] | None = None,
) -> QueryPlan:
    """Validate an invocation in process and resolve what it would request.

    This is the normative boundary, not the JSON schema: model arguments are
    splatted straight into the tool's ``_run``, and whether the SDK's MCP layer
    enforces ``input_schema`` carries a standing "verify at deploy" note in this
    codebase. Nothing here builds a request until every argument is a member of a
    closed set.

    Raises :class:`QueryRefused` for anything out of domain — never a narrowed
    query, a substituted value, or an empty result standing in for a rejection.
    """
    arguments = dict(arguments or {})
    if query_name not in QUERY_REGISTRY:
        raise QueryRefused(
            f"unknown query_name {query_name!r}. homelab_query answers a closed "
            f"set of six named queries: {', '.join(QUERY_NAMES)}. No request was "
            "issued."
        )
    entry = QUERY_REGISTRY[query_name]
    declared = {parameter.name for parameter in entry.parameters}

    unknown = sorted(set(arguments) - declared)
    if unknown:
        accepted = ", ".join(sorted(declared)) or "no parameters"
        raise QueryRefused(
            f"{entry.name} does not accept {', '.join(unknown)}. It takes "
            f"{accepted}. An unrecognised argument is refused rather than "
            "dropped: dropping it would answer a different question than the "
            "one asked. No request was issued."
        )
    missing = sorted(declared - set(arguments))
    if missing:
        raise QueryRefused(
            f"{entry.name} requires {', '.join(missing)}. No request was issued."
        )

    values: dict[str, str] = {}
    for parameter in entry.parameters:
        supplied = arguments[parameter.name]
        domain = parameter.domain
        if domain is None:
            domain = (discovered or {}).get(parameter.name)
            if domain is None:
                raise QueryRefused(
                    f"{entry.name}'s {parameter.name} domain has not been "
                    "discovered from the backend, so no value can be validated "
                    "against it. Failing closed rather than passing the argument "
                    "through: no request was issued."
                )
        if not isinstance(supplied, str) or supplied not in domain:
            raise QueryRefused(
                f"{entry.name} does not accept {parameter.name}={supplied!r}: "
                f"it is outside this query's domain, which is "
                f"{', '.join(domain) or 'empty'}"
                + (f" ({parameter.note})" if parameter.note else "")
                + ". No request was issued, and no empty result is returned in "
                "place of this refusal."
            )
        values[parameter.name] = supplied

    caveats = list(entry.caveats)
    for hole in entry.unavailable:
        if not all(values.get(k) == v for k, v in hole.parameters.items()):
            continue
        if hole.aspect is None:
            described = ", ".join(f"{k}={v}" for k, v in hole.parameters.items())
            return QueryPlan(
                entry=entry,
                arguments=values,
                outcome=QueryOutcome.NOT_DERIVABLE,
                message=(
                    f"{entry.name} for {described} is inside this query's domain "
                    f"but cannot be derived: {hole.reason} This is not an empty "
                    "measurement and not a rejected argument — there is no series "
                    "to read."
                ),
                caveats=tuple(caveats),
                window=values.get("window"),
            )
        caveats.append(f"{hole.aspect} is unavailable here: {hole.reason}")

    job = entry.job_map.get(values["node"]) if entry.job_map else None
    fields = {k: v for k, v in values.items()}
    if job:
        fields["job"] = job
    roles = (
        {values[entry.select_by]: entry.expressions[values[entry.select_by]]}
        if entry.select_by
        else dict(entry.expressions)
    )
    return QueryPlan(
        entry=entry,
        arguments=values,
        outcome=QueryOutcome.ANSWERED,
        expressions={
            role: _fill(template, fields, encode=False)
            for role, template in roles.items()
        },
        routes={
            role: _fill(route, fields, encode=True)
            for role, route in entry.routes.items()
        },
        caveats=tuple(caveats),
        window=values.get("window"),
    )


# --- Projection: results name jobs, never addresses ------------------------


def project_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """Drop every address-bearing label from a backend's own label set.

    Two filters, because either alone leaks. The name denylist catches the six
    labels measured on live responses; the value-shape check catches the label
    nobody has seen yet, which is how the next address would arrive.
    """
    projected: dict[str, str] = {}
    for name, value in labels.items():
        if name.startswith("__"):
            continue
        if name.lower() in _ADDRESS_BEARING_LOWER:
            continue
        if isinstance(value, str) and _is_address_shaped(value):
            continue
        projected[name] = value
    return projected


def friendly_target(job: str) -> str:
    """The node enum value for a job, or the job label when it has no node.

    `adguard-exporter` and `pushgateway` have no node of their own, so they are
    named by their job — which is still address-free.
    """
    return NODE_FOR_JOB.get(job, job)


def describe_target(job: str, labels: Mapping[str, str] | None = None) -> str:
    """Name a target by its friendly value, its job, and its safe labels."""
    projected = project_labels(labels or {})
    projected.pop("job", None)
    parts = [f"job={job}"] + [f"{k}={v}" for k, v in sorted(projected.items())]
    return f"{friendly_target(job)} ({', '.join(parts)})"
