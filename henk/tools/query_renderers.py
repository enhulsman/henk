"""The six named queries' renderers: a backend response becomes owner-readable.

Each registry entry points at the renderer named after it, so the mapping is
checkable by inspection and a copy-paste that aims two entries at one renderer
fails a test rather than silently answering the wrong question. The signature is
fixed:

    render_<query_name>(plan: QueryPlan, payloads: Mapping[str, Any]) -> str

``payloads`` is keyed by the plan's expression and route roles, holding each
backend response as parsed JSON.

Four rules hold across all six, and each has a named failure mode:

**No address, ever.** Every label set goes through :func:`project_labels` and
every backend-authored string through :func:`scrub_addresses`. The second is not
belt-and-braces: Prometheus's `lastError` — which the spec requires surfacing —
almost always quotes the scrape URL, so a renderer that only filtered labels
would publish an address in the one field the owner most wants to read.

**A summary, never the series.** The two range queries report first / last / min
/ max and a direction. The samples between them never reach the result at any
window.

**Three outcomes stay three.** A response that comes back empty for an in-domain
request renders as *not derivable*, not as a measurement of nothing — the same
outcome the registry short-circuits for the holes it already knows about
(`temperature` on the vps). An empty container list and "no containers" are
different statements, and only one of them is true.

**Every caveat travels with the entry.** A renderer prints ``plan.caveats``
rather than remembering the statements itself, so a statement cannot be dropped
by editing one function — which matters most for the two that a reader cannot
infer from the figures: container creation-time semantics, and what the container
list silently omits.

Ages are derived from the **evaluation timestamp Prometheus stamps on each
sample**, never from the local clock. That is what makes a dead writer visible:
its raw timestamp freezes while the evaluation time advances, so the reported age
grows across invocations instead of standing still.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from urllib.parse import urlsplit

# Only `query_projection`, never `query_registry`: the registry imports THIS
# module to bind each entry's renderer, so an import back the other way is a
# cycle. Everything a renderer needs from the registry side arrives on the
# `plan` it is handed.
from henk.tools.query_projection import (
    NODE_FOR_JOB,
    describe_target,
    project_labels,
    scrub_addresses,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from henk.tools.query_registry import QueryPlan

_PENDING = (
    "its renderer is not implemented yet. The query was validated and "
    "dispatched, but there is nothing here to summarise it."
)

#: How each resource's figure reads. Presentation only — the *thresholds* live in
#: the registry, pinned to live rule expressions.
_RESOURCE_UNITS: Mapping[str, str] = {
    "cpu": "percent busy",
    "memory": "percent used",
    "disk": "percent free",
    "load": "load average (1m)",
    "swap_used": "percent full",
    "swap_io": "pages/s",
    "temperature": "°C",
}


def render_unimplemented(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """Default slot: an entry that forgot to name its renderer."""
    raise NotImplementedError(_PENDING)


# --- Shared shape handling -------------------------------------------------


def _result(payload: Any) -> list[Mapping[str, Any]]:
    """`data.result` from a Prometheus response, tolerantly.

    Tolerant on purpose: a renderer must never raise on a shape it did not
    expect. A malformed response is an honest empty result — which lands on the
    not-derivable branch — not a crashed turn.
    """
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    result = data.get("result")
    if not isinstance(result, list):
        return []
    return [series for series in result if isinstance(series, Mapping)]


def _labels(series: Mapping[str, Any]) -> dict[str, str]:
    metric = series.get("metric")
    return {k: v for k, v in metric.items() if isinstance(v, str)} if isinstance(metric, Mapping) else {}


def _pairs(raw: Any) -> tuple[float, float] | None:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) < 2:
        return None
    try:
        return float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None


def _instant(series: Mapping[str, Any]) -> tuple[float, float] | None:
    """(evaluation timestamp, value) of an instant-vector sample."""
    return _pairs(series.get("value"))


def _points(series: Mapping[str, Any]) -> list[tuple[float, float]]:
    """(timestamp, value) points of a range series, or the single instant one."""
    values = series.get("values")
    if isinstance(values, list):
        parsed = [_pairs(point) for point in values]
        return [point for point in parsed if point is not None]
    single = _instant(series)
    return [single] if single else []


@dataclass(frozen=True)
class _Summary:
    first: float
    last: float
    minimum: float
    maximum: float
    count: int

    @property
    def direction(self) -> str:
        delta = self.last - self.first
        if abs(delta) < 1e-9:
            return "flat"
        return "rising" if delta > 0 else "falling"


def _summarise(points: Sequence[tuple[float, float]], *, scale: float = 1.0) -> _Summary:
    values = [value * scale for _ts, value in points]
    return _Summary(
        first=values[0],
        last=values[-1],
        minimum=min(values),
        maximum=max(values),
        count=len(values),
    )


def _number(value: float) -> str:
    return f"{value:.2f}"


def _summary_line(summary: _Summary, unit: str) -> str:
    return (
        f"Summary: first {_number(summary.first)}, last {_number(summary.last)}, "
        f"min {_number(summary.minimum)}, max {_number(summary.maximum)} {unit} "
        f"over {summary.count} points — {summary.direction}."
    )


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_hours(now: float, then: float) -> float:
    return max(0.0, now - then) / 3600.0


def _not_derivable(plan: "QueryPlan", detail: str) -> str:
    """The third outcome, reached at runtime rather than from the record.

    Deliberately worded so it cannot be read as either of the other two: it is
    not a rejected argument, and it is not a measurement that came back with
    nothing in it.
    """
    described = ", ".join(f"{k}={v}" for k, v in plan.arguments.items()) or "no parameters"
    return "\n".join(
        [
            f"{plan.entry.name} ({described}): could not be derived — {detail}",
            "The argument was accepted and the query ran; there was no series to "
            "summarise. That is not a reading of zero, and not a rejection.",
        ]
    )


def _multiplicity(plan: "QueryPlan", job: str, found: Sequence[Mapping[str, Any]], subject: str) -> str:
    label_sets = "; ".join(
        "{" + ", ".join(f"{k}={v}" for k, v in sorted(project_labels(_labels(s)).items())) + "}"
        for s in found
    )
    return "\n".join(
        [
            f"{plan.entry.name} — {subject}: job {job} returned {len(found)} series "
            "where exactly one was expected.",
            "No single figure is presented for it: one of several series rendered "
            "as the target's measurement would be a confident wrong answer.",
            f"Their non-address labels are: {label_sets}. Any further difference "
            "lies in labels this tool drops because they carry addresses.",
        ]
    )


def _caveats(plan: "QueryPlan") -> list[str]:
    if not plan.caveats:
        return []
    return ["Caveats:"] + [f"- {caveat}" for caveat in plan.caveats]


def _threshold_line(plan: "QueryPlan", key: str, summary: _Summary) -> str | None:
    """The per-resource comparison, or None where no rule defines a bar.

    Per-resource because the fleet's rules do not share a shape: a generic
    "crossed its alert threshold" would report disk backwards (the rule is
    percent *free* below a bar) and would present swap fullness — which is not
    what the rule fires on — as an approaching incident.
    """
    bar = plan.entry.thresholds.get(key)
    if bar is None:
        return None
    reading = summary.maximum if bar.direction == "above" else summary.minimum
    crossed = bar.crossed_by(reading)
    verdict = (
        f"the {'highest' if bar.direction == 'above' else 'lowest'} reading "
        f"{_number(reading)} {'crossed' if crossed else 'stayed clear of'} it"
    )
    line = (
        f"Compared against {_number(bar.value)} {bar.unit} ({bar.direction} the "
        f"bar), pinned from the live rule `{bar.source}`: {verdict}."
    )
    if bar.note:
        line += f" {bar.note}"
    return line


# --- node_resource_trend ---------------------------------------------------


def render_node_resource_trend(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """first/last/min/max, direction, and the per-resource comparison."""
    node = plan.arguments["node"]
    resource = plan.arguments["resource"]
    window = plan.arguments["window"]
    job = plan.entry.job_map[node] if plan.entry.job_map else ""
    found = _result(payloads.get(resource))

    if not found:
        return _not_derivable(
            plan,
            f"job {job} returned no series for {resource} over {window}",
        )
    if len(found) > 1 and resource in plan.entry.single_series:
        return _multiplicity(plan, job, found, f"{node} {resource} over {window}")

    points = _points(found[0])
    if not points:
        return _not_derivable(plan, f"job {job} returned a series with no samples")

    summary = _summarise(points)
    unit = _RESOURCE_UNITS.get(resource, "")
    lines = [
        f"node_resource_trend — {node} {resource} over {window}: "
        f"{describe_target(job, _labels(found[0]))}",
        _summary_line(summary, unit),
    ]
    comparison = _threshold_line(plan, resource, summary)
    if comparison is None:
        lines.append(
            f"No bar: no alert rule in either alerting system defines a threshold "
            f"for {resource}, so this is the figure and its direction, nothing more."
        )
    else:
        lines.append(comparison)
        if not plan.entry.thresholds[resource].is_trigger:
            lines.append(
                "Read this as normal for this fleet unless something else says "
                "otherwise: swap pressure is what the rule triggers on, and "
                "pressure is measured by swap_io (pages/s), not by fullness."
            )
    lines.extend(_caveats(plan))
    return "\n".join(lines)


# --- scrape_targets --------------------------------------------------------


def _errors_by_job(payload: Any) -> dict[str, list[str]]:
    """`lastError` per job, scrubbed. `scrapeUrl`/`globalUrl` are never read."""
    errors: dict[str, list[str]] = {}
    if not isinstance(payload, Mapping):
        return errors
    data = payload.get("data")
    targets = data.get("activeTargets") if isinstance(data, Mapping) else None
    if not isinstance(targets, list):
        return errors
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        labels = target.get("labels")
        job = labels.get("job") if isinstance(labels, Mapping) else None
        message = target.get("lastError")
        if not isinstance(job, str) or not isinstance(message, str) or not message:
            continue
        errors.setdefault(job, []).append(scrub_addresses(message))
    return errors


def render_scrape_targets(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """Every target with its `up` value, plus `lastError` for the down ones."""
    found = _result(payloads.get("up"))
    if not found:
        return _not_derivable(
            plan,
            "bare `up` returned no series at all, which means the query did not "
            "reach a Prometheus holding scrape data",
        )

    window = plan.entry.lookback or "the query window"
    ever_up = {
        _labels(series).get("job", ""): (_instant(series) or (0.0, 0.0))[1]
        for series in _result(payloads.get("up_over_window"))
    }
    errors = _errors_by_job(payloads.get("targets"))

    rows: list[str] = []
    down = 0
    for series in found:
        labels = _labels(series)
        job = labels.get("job", "unknown")
        reading = _instant(series)
        value = reading[1] if reading else None
        target = describe_target(job, labels)
        if value is None:
            rows.append(f"  ?    {target} — no value in the response")
            continue
        if value >= 1:
            rows.append(f"  UP   {target} — up=1")
            continue
        down += 1
        rows.append(f"  DOWN {target} — up=0")
        for message in errors.get(job, []) or ["no scrape error recorded by the backend"]:
            rows.append(f"         last scrape error: {message}")
        if ever_up.get(job, 0.0) >= 1:
            rows.append(
                f"         it was up at some point within the last {window}; this "
                "query cannot say when."
            )
        else:
            rows.append(
                f"         not up at any point within the last {window}: down for "
                "longer than the window, and no specific duration is available."
            )

    header = (
        f"scrape_targets — {len(found)} targets, {down} down. Bare `up` is "
        "evaluated and every target is listed with its value, so a healthy fleet "
        "is visibly healthy rather than indistinguishable from a broken query."
    )
    return "\n".join([header, *rows, *_caveats(plan)])


# --- endpoint_history ------------------------------------------------------


def _event_list(payload: Any) -> list[Mapping[str, Any]]:
    events = payload.get("events") if isinstance(payload, Mapping) else None
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, Mapping)]


def _check_list(payload: Any) -> list[Mapping[str, Any]]:
    # A page past the end returns JSON null, not []. `len(None)` would raise.
    results = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(results, list):
        return []
    return [check for check in results if isinstance(check, Mapping)]


def render_endpoint_history(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """Current state, failing condition, since-when, and the uptime ratio."""
    payload = payloads.get("statuses")
    endpoint = plan.arguments.get("endpoint", "")
    window = plan.arguments.get("window", "")
    if not isinstance(payload, Mapping):
        return _not_derivable(plan, "Gatus returned no status object for this endpoint")

    checks = _check_list(payload)
    events = _event_list(payload)
    group = payload.get("group")
    name = payload.get("name")
    described = f" ({group} / {name})" if isinstance(group, str) and isinstance(name, str) else ""
    lines = [f"endpoint_history — {endpoint}{described} over {window}"]

    if checks:
        latest = checks[-1]
        healthy = bool(latest.get("success"))
        duration = latest.get("duration")
        latency = (
            f", {float(duration) / 1_000_000:.0f} ms"
            if isinstance(duration, (int, float))
            else ""
        )
        lines.append(
            f"Current state: {'passing' if healthy else 'FAILING'} (last check "
            f"{scrub_addresses(str(latest.get('timestamp', 'unknown')))}, HTTP "
            f"{latest.get('status', 'unknown')}{latency})"
        )
        failing = [
            scrub_addresses(str(condition.get("condition", "")))
            for condition in latest.get("conditionResults") or []
            if isinstance(condition, Mapping) and not condition.get("success")
        ]
        if failing:
            lines.append("Failing condition: " + "; ".join(failing))
    else:
        lines.append(
            "Current state: no check results stored — Gatus keeps only about 1.7 "
            "hours of them, and resets its history on a config change."
        )

    # "Since when" comes from the transition events, never from the results: the
    # results window is ~1.7 hours, so its oldest entry can only ever answer
    # "since at most 1.7 hours ago", confidently and wrongly.
    transitions = [
        event
        for event in events
        if str(event.get("type", "")).upper() in {"HEALTHY", "UNHEALTHY"}
    ]
    if transitions:
        last = transitions[-1]
        lines.append(
            f"Since: {scrub_addresses(str(last.get('timestamp', 'unknown')))} — the "
            f"last {str(last.get('type', '')).upper()} transition, taken from the "
            "events log rather than from the check results."
        )
        lines.append(
            f"Transitions recorded: {len(transitions)} across "
            f"{len(events)} events."
        )
    else:
        lines.append(
            "Since: unknown — no transition events are recorded for this endpoint, "
            "so there is no state change to date. None is invented here."
        )

    uptime = payloads.get("uptime")
    if isinstance(uptime, (int, float)) and not isinstance(uptime, bool):
        lines.append(f"Uptime over {window}: {float(uptime) * 100:.2f}%")
    else:
        lines.append(f"Uptime over {window}: not available from the backend.")

    lines.extend(_caveats(plan))
    return "\n".join(lines)


# --- freshness_check -------------------------------------------------------


def render_freshness_check(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """Each pipeline's raw timestamp beside the age derived from it."""
    found = _result(payloads.get("timestamps"))
    if not found:
        return _not_derivable(
            plan, "none of the eight timestamp metrics returned a series"
        )

    rows: list[str] = []
    for series in found:
        labels = _labels(series)
        metric = labels.get("__name__", "unknown metric")
        reading = _instant(series)
        if reading is None:
            rows.append(f"  {metric}: no value in the response")
            continue
        evaluated_at, raw = reading
        job = labels.get("job", "")
        target = describe_target(job, labels) if job else "unknown target"
        rows.append(
            f"  {metric} — {target}: raw timestamp {raw:.0f} "
            f"({_stamp(raw)}) → {_age_hours(evaluated_at, raw):.2f} h ago"
        )

    header = (
        f"freshness_check — {len(found)} timestamp series. Each pipeline's RAW "
        "timestamp is reported beside the age derived from it, so a writer that "
        "has died shows an age that keeps growing rather than one that freezes."
    )
    return "\n".join([header, *rows, *_caveats(plan)])


# --- container_state -------------------------------------------------------


def _by_container(payload: Any) -> dict[str, tuple[float, float]]:
    readings: dict[str, tuple[float, float]] = {}
    for series in _result(payload):
        name = _labels(series).get("name")
        reading = _instant(series)
        if isinstance(name, str) and name and reading is not None:
            readings[name] = reading
    return readings


def render_container_state(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """Per-container last-seen, health, OOM events and creation time."""
    node = plan.arguments["node"]
    last_seen = _by_container(payloads.get("last_seen"))
    created = _by_container(payloads.get("created"))
    oom = _by_container(payloads.get("oom_events"))
    health = _by_container(payloads.get("health_state"))

    names = sorted(set(last_seen) | set(created) | set(oom) | set(health))
    if not names:
        return _not_derivable(
            plan,
            "the container-metrics exporter returned no container series for this "
            "node — which is not the same statement as 'no containers are running'",
        )

    rows: list[str] = []
    for name in names:
        parts: list[str] = []
        if name in last_seen:
            evaluated_at, value = last_seen[name]
            parts.append(f"last seen {max(0.0, evaluated_at - value):.0f} s ago")
        if name in health:
            parts.append(f"health state {health[name][1]:.0f} (the exporter's own encoding)")
        else:
            parts.append("health state unavailable")
        if name in oom:
            parts.append(f"OOM events {oom[name][1]:.0f}")
        if name in created:
            parts.append(
                f"created {_stamp(created[name][1])} — creation time, not its last start"
            )
        rows.append(f"  {scrub_addresses(name)}: " + "; ".join(parts))

    job = plan.entry.job_map.get(node, "") if plan.entry.job_map else ""
    header = (
        f"container_state — {describe_target(job, {})}: {len(names)} containers "
        "currently reporting."
    )
    return "\n".join([header, *rows, *_caveats(plan)])


def render_named_container_reading(node: str, container: str, payload: Any) -> str:
    """Interpret the `or vector()` form's answer for one named container.

    The whole point of that form is that absence produces a *value*: without it
    the response is an empty series, which reads as "nothing to report" about the
    exact container the owner is asking after.
    """
    found = _result(payload)
    if not found:
        return (
            f"{container} on {node}: the guarded expression returned no series at "
            "all, which should not happen — `or vector()` guarantees a value. "
            "Treat this as a backend problem, not as a statement about the "
            "container."
        )
    reading = _instant(found[0])
    labels = _labels(found[0])
    if reading is None:
        return f"{container} on {node}: no value in the response."
    _evaluated_at, value = reading
    if not labels.get("name") or value <= 0:
        return (
            f"{container} on {node}: absent from the container-metrics exporter. "
            "The exporter drops a container's series entirely when it stops, so "
            "this container may be stopped or removed — the reading is the "
            "guard's fallback value, not a measurement of the container."
        )
    return (
        f"{container} on {node}: last seen {_stamp(value)} "
        f"({describe_target(labels.get('job', ''), labels)})."
    )


# --- dns_performance -------------------------------------------------------


def _host_of(value: str) -> str:
    """Host part of an `addr:port` or a `scheme://addr:port` string.

    The values handled here are tailnet addresses. They exist in this process's
    memory for the length of one match and reach no result, no log line, and no
    file — which is the whole reason the mapping is derived rather than stored.
    """
    if "://" in value:
        return urlsplit(value).hostname or ""
    return value.rsplit(":", 1)[0] if ":" in value else value


def _derive_node_hosts(payload: Any) -> dict[str, str]:
    """node enum value -> host, from `up{job=~"node-exporter.*"}`."""
    hosts: dict[str, str] = {}
    for series in _result(payload):
        labels = _labels(series)
        job = labels.get("job", "")
        instance = labels.get("instance", "")
        node = NODE_FOR_JOB.get(job)
        if node and instance:
            hosts[node] = _host_of(instance)
    return hosts


def render_dns_performance(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """The derived node mapping, the summary, and the measured baseline."""
    node = plan.arguments["node"]
    window = plan.arguments["window"]
    node_hosts = _derive_node_hosts(payloads.get("node_mapping"))
    host = node_hosts.get(node)
    if not host:
        return _not_derivable(
            plan,
            f"{node}'s node-exporter series are absent, so the AdGuard series "
            "belonging to it cannot be identified. The mapping is derived at "
            "query time from job-labelled series and is deliberately stored "
            "nowhere, so there is no fallback table to fall back to",
        )

    found = [
        series
        for series in _result(payloads.get("series"))
        if _host_of(_labels(series).get("server", "")) == host
    ]
    if not found:
        return _not_derivable(
            plan,
            f"no AdGuard series matched {node}'s node-exporter host, so this "
            "node's resolver latency cannot be attributed",
        )
    if len(found) > 1:
        return _multiplicity(plan, "adguard-exporter", found, f"{node} over {window}")

    points = _points(found[0])
    if not points:
        return _not_derivable(plan, f"{node}'s AdGuard series carried no samples")

    # Seconds on the wire, milliseconds everywhere the owner reads them — the
    # rules are written in seconds and the record's headroom table in ms.
    summary = _summarise(points, scale=1000.0)
    baseline = plan.entry.baselines.get(node)
    warning = plan.entry.thresholds.get(f"{node}_warning")
    critical = plan.entry.thresholds.get(f"{node}_critical")
    fleet = plan.entry.thresholds.get("fleet_critical")

    lines = [
        f"dns_performance — {node} over {window}. The node-to-series mapping is "
        "derived at query time from job-labelled series; no address is stored in "
        "the repository or in configuration, and none appears below.",
        _summary_line(summary, "ms"),
    ]
    if baseline is not None:
        drift = summary.last - baseline
        lines.append(
            f"Measured 24h baseline for {node}: {_number(baseline)} ms — the last "
            f"reading is {_number(abs(drift))} ms "
            f"{'above' if drift >= 0 else 'below'} it."
        )
    bars = [
        f"{label} {_number(bar.value)} {bar.unit} (`{bar.source}`)"
        for label, bar in (("warning", warning), ("critical", critical), ("fleet-wide critical", fleet))
        if bar is not None
    ]
    if bars:
        crossings = [
            label
            for label, bar in (("warning", warning), ("critical", critical), ("fleet-wide critical", fleet))
            if bar is not None and summary.maximum > bar.value
        ]
        verdict = (
            "crossed: " + ", ".join(crossings)
            if crossings
            else f"the highest reading {_number(summary.maximum)} stayed clear of all of them"
        )
        lines.append(f"Bars for {node}: " + "; ".join(bars) + f" — {verdict}.")
    lines.extend(_caveats(plan))
    return "\n".join(lines)
