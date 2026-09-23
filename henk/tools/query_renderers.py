"""The named queries' renderers: a backend response becomes owner-readable.

Each registry entry points at the renderer named after it, so the mapping is
checkable by inspection and a copy-paste that aims two entries at one renderer
fails a test rather than silently answering the wrong question. The signature is
fixed:

    render_<query_name>(plan: QueryPlan, payloads: Mapping[str, Any]) -> str

``payloads`` is keyed by the plan's expression and route roles, holding each
backend response as parsed JSON.

Four rules hold across all of them, and each has a named failure mode:

**No address, ever.** Every label set goes through :func:`project_labels` and
every backend-authored string through :func:`scrub_addresses`. The second is not
belt-and-braces: Prometheus's `lastError` — which the spec requires surfacing —
almost always quotes the scrape URL, so a renderer that only filtered labels
would publish an address in the one field the owner most wants to read.

**A summary, never the series.** The two trend queries report first / last / min
/ max and a direction, each figure with the UTC time it refers to, plus the
window's end — the request's end, carried on the plan, never the last sample.
The samples between them never reach the result at any window. The two
host-coverage queries (`memory_movers`, `host_service_state`) read a range too,
and report a ranking or per-unit sample counts from it, never its samples.

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

import re
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
    from henk.tools.query_registry import QueryPlan, RangeWindow

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
    """first / last / min / max, each with the evaluation time it refers to.

    ``min_at``/``max_at`` are when the extreme was FIRST reached, and
    ``min_last_at``/``max_last_at`` when it was last reached. Both are kept
    because either alone hides something: earliest-only hides that a figure is
    *still* at its extreme, latest-only hides when it got there (D1). The
    window's end is NOT a property of the series: it is the request's end, and
    arrives on the plan (see :func:`_window_lines`).
    """

    first: float
    last: float
    minimum: float
    maximum: float
    count: int
    first_at: float
    last_at: float
    min_at: float
    min_last_at: float
    max_at: float
    max_last_at: float

    @property
    def direction(self) -> str:
        delta = self.last - self.first
        if abs(delta) < 1e-9:
            return "flat"
        return "rising" if delta > 0 else "falling"


def _summarise(points: Sequence[tuple[float, float]], *, scale: float = 1.0) -> _Summary:
    scaled = [(ts, value * scale) for ts, value in points]
    values = [value for _ts, value in scaled]
    minimum, maximum = min(values), max(values)
    at_min = [ts for ts, value in scaled if value == minimum]
    at_max = [ts for ts, value in scaled if value == maximum]
    return _Summary(
        first=values[0],
        last=values[-1],
        minimum=minimum,
        maximum=maximum,
        count=len(values),
        first_at=scaled[0][0],
        last_at=scaled[-1][0],
        min_at=at_min[0],
        min_last_at=at_min[-1],
        max_at=at_max[0],
        max_last_at=at_max[-1],
    )


def _number(value: float) -> str:
    return f"{value:.2f}"


def _extreme(value: float, first_at: float, last_at: float) -> str:
    if first_at == last_at:
        return f"{_number(value)} at {_stamp(first_at)}"
    return (
        f"{_number(value)}, first reached at {_stamp(first_at)} and last reached "
        f"at {_stamp(last_at)}"
    )


def _summary_line(summary: _Summary, unit: str, window: "RangeWindow | None") -> str:
    """The one summary line both range queries share, every figure with its time.

    The window's end is the request's end, carried on the plan. It is never the
    series' last sample: a series that stopped reporting 20 minutes early would
    then read as current to the end of the window, which is exactly the node
    going down mid-incident that a triage reader must not miss.
    """
    end = (
        f"Window ends at {_stamp(window.end)}."
        if window is not None
        else "The window's end time was not supplied with this plan, so no end is stated."
    )
    return (
        f"Summary: {unit}, UTC times — "
        f"first {_number(summary.first)} at {_stamp(summary.first_at)}; "
        f"last {_number(summary.last)} at {_stamp(summary.last_at)}; "
        f"min {_extreme(summary.minimum, summary.min_at, summary.min_last_at)}; "
        f"max {_extreme(summary.maximum, summary.max_at, summary.max_last_at)}. "
        f"{summary.count} points, {summary.direction}. "
        f"{end}"
    )


def _duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60.0))
    if minutes < 120:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds / 3600.0:.1f} hours"


def _short_of_last_point(last_at: float, window: "RangeWindow") -> bool:
    """Whether a series' last sample falls short of the window's last range point.

    THE "not present at the window's end" rule, shared by every range renderer.
    It is judged against the last range point, never against ``end``: when the
    step does not divide the window the last point lies before ``end`` (1430 s
    for 24h at 60 points), so a check against ``end`` would call every healthy
    24h series silent. Half a step of tolerance absorbs Prometheus's
    millisecond timestamps.
    """
    return last_at < window.last_point - window.step / 2


def _window_lines(summary: _Summary, window: "RangeWindow | None") -> list[str]:
    """What the window's end says about the series, beyond the summary line.

    A series whose last sample falls short of the last range point stopped
    reporting: say for how long, measured to the window's end. Half a step of
    tolerance absorbs Prometheus's millisecond timestamps. A series current to
    the last range point is not silent, but when the step does not divide the
    window that point can lie well before the end (1430 s for 24h at 60
    points), and when the difference is a minute or more the line says so.
    """
    if window is None:
        return []
    if _short_of_last_point(summary.last_at, window):
        return [
            f"No sample for the last {_duration(window.end - summary.last_at)} of the "
            f"window: the series' last sample is at {_stamp(summary.last_at)} and the "
            f"window ends at {_stamp(window.end)}. The figures above describe the "
            "series up to its last sample, not the state at the window's end."
        ]
    if window.end - window.last_point >= 60.0:
        return [
            f"The last range point is at {_stamp(window.last_point)}: the "
            f"{window.step}s step does not divide the window evenly, so nothing "
            "after that time was evaluated."
        ]
    return []


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


#: Said where the record pins no `for` for a rule. Never replaced by a guess.
FOR_NOT_PINNED = "the rule's `for` is not in the pinned record"


def _for_clause(bar: Any) -> str:
    """The rule's `for` window, or the statement that the record has none."""
    if bar.for_window:
        return f"which fires once the condition holds for {bar.for_window}"
    return FOR_NOT_PINNED


def _threshold_line(plan: "QueryPlan", key: str, summary: _Summary) -> list[str]:
    """The per-resource comparison, or [] where no rule defines a bar.

    Per-resource because the fleet's rules do not share a shape: a generic
    "crossed its alert threshold" would report disk backwards (the rule is
    percent *free* below a bar).

    A bar that is one ``branch`` of an OR'd rule (`HenkSwapPressure`) names its
    branch, says what it alone can or cannot do, and adds the fixed line that
    the rule fires on either branch. Neither branch is ranked as the real
    trigger: the ground truth of 2026-09-23 contradicts any ranking (D2). A
    sub-bar figure is still never presented as an approaching incident — the
    clear-bar sentence carries that.
    """
    bar = plan.entry.thresholds.get(key)
    if bar is None:
        return []
    reading = summary.maximum if bar.direction == "above" else summary.minimum
    crossed = bar.crossed_by(reading)
    verdict = (
        f"the {'highest' if bar.direction == 'above' else 'lowest'} reading "
        f"{_number(reading)} {'crossed' if crossed else 'stayed clear of'} it"
    )
    rule = (
        f"the {bar.branch} branch of the live rule `{bar.source}`"
        if bar.branch
        else f"pinned from the live rule `{bar.source}`"
    )
    line = (
        f"Compared against {_number(bar.value)} {bar.unit} ({bar.direction} the "
        f"bar), {rule}, {_for_clause(bar)}: {verdict}"
    )
    if bar.branch:
        sustained = f"once sustained for {bar.for_window}" if bar.for_window else (
            f"once sustained ({FOR_NOT_PINNED})"
        )
        line += (
            f" — this branch alone can fire `{bar.source}` {sustained}."
            if crossed
            else " — below this branch's bar; on its own this branch would not "
            "fire the rule."
        )
    else:
        line += "."
    if bar.note:
        line += f" {bar.note}"
    lines = [line]
    if bar.branch:
        siblings = [
            name
            for name, other in plan.entry.thresholds.items()
            if other.source == bar.source and other.branch
        ]
        lines.append(
            f"`{bar.source}` fires on either branch, and the alert's value does not "
            "say which fired; check both "
            + " and ".join(f"`{name}`" for name in siblings)
            + "."
        )
    return lines


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
        _summary_line(summary, unit, plan.range_window),
        *_window_lines(summary, plan.range_window),
    ]
    comparison = _threshold_line(plan, resource, summary)
    if not comparison:
        lines.append(
            f"No bar: no alert rule in either alerting system defines a threshold "
            f"for {resource}, so this is the figure and its direction, nothing more."
        )
    else:
        lines.extend(comparison)
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


#: A down target's error part when the targets payload cannot exist: a rebuilt or
#: replayed case has no historical targets API (triage-quality D15).
TARGETS_UNAVAILABLE_IN_RECONSTRUCTION = (
    "unavailable in reconstruction: the targets API has no historical form"
)


def render_scrape_targets(
    plan: "QueryPlan",
    payloads: Mapping[str, Any],
    *,
    targets_unavailable: bool = False,
) -> str:
    """Every target with its `up` value, plus `lastError` for the down ones.

    ``targets_unavailable`` is the reconstruction seam (triage-quality D15): a
    missing targets payload would otherwise read as "no scrape error recorded by
    the backend", which is false for a case rebuilt from a capture. With it set,
    each down target's error part says the targets API is unavailable instead.
    Live dispatch never sets it, so live output is unchanged.
    """
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
        messages = (
            [TARGETS_UNAVAILABLE_IN_RECONSTRUCTION]
            if targets_unavailable
            else errors.get(job, []) or ["no scrape error recorded by the backend"]
        )
        for message in messages:
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


#: Docker's generated container names: `<adjective>_<surname>`, lowercase, with
#: one retry digit appended on a collision (moby `pkg/namesgenerator`).
_AUTO_NAME = re.compile(r"[a-z]+_[a-z]+\d?")

_MIB = 1024.0 * 1024.0


def auto_name_annotation(name: str) -> str | None:
    """The hedged note for a name in Docker's auto-generated shape, else None.

    Hedged on purpose: a hand-chosen `my_app` has the same shape, so the note
    says what the name *probably* means and that the shape is not proof. Shared
    with any renderer that names containers (D3, D5).
    """
    if not _AUTO_NAME.fullmatch(name):
        return None
    return (
        "name looks auto-generated (Docker's adjective_surname form): probably an "
        "ephemeral `docker run` container, such as the nightly backup's "
        "`docker run --rm`; a hand-chosen name can share this form"
    )


def _clause(reason: str) -> str:
    """A hole's reason as a clause inside a `;`-separated row."""
    return reason.strip().rstrip(".")


def _mib(value: float) -> str:
    return f"{value / _MIB:.1f} MiB"


def _top(readings: Mapping[str, tuple[float, float]], count: int = 3) -> str:
    ranked = sorted(readings.items(), key=lambda item: (-item[1][1], item[0]))[:count]
    if not ranked:
        return "no series returned"
    return ", ".join(f"{scrub_addresses(name)} ({_mib(value)})" for name, (_ts, value) in ranked)


def render_container_state(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """Per-container last-seen, health, OOM events, memory, swap, restarts, creation.

    Every aspect-level hole is **read from the plan** and printed in that
    aspect's own column, with its reason, in every row. A missing reading where
    no hole is registered says so in its own words; neither ever renders as an
    empty or zero column (D3).
    """
    node = plan.arguments["node"]
    holes = plan.unavailable_aspects
    last_seen = _by_container(payloads.get("last_seen"))
    created = _by_container(payloads.get("created"))
    oom = _by_container(payloads.get("oom_events"))
    health = _by_container(payloads.get("health_state"))
    working_set = _by_container(payloads.get("memory_working_set"))
    swap = _by_container(payloads.get("swap"))
    restarts_15m = _by_container(payloads.get("restarts_15m"))
    restarts_24h = _by_container(payloads.get("restarts_24h"))

    names = sorted(
        set(last_seen) | set(created) | set(oom) | set(health) | set(working_set) | set(swap)
    )
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
        if "health_state" in holes:
            parts.append(f"health state unavailable: {_clause(holes['health_state'])}")
        elif name in health:
            parts.append(f"health state {health[name][1]:.0f} (the exporter's own encoding)")
        else:
            parts.append("health state: no series for this container")
        if name in oom:
            parts.append(f"OOM events {oom[name][1]:.0f}")
        parts.append(
            f"working set {_mib(working_set[name][1])}"
            if name in working_set
            else "working set: no series"
        )
        parts.append(f"swap {_mib(swap[name][1])}" if name in swap else "swap: no series")
        if "restarts" in holes:
            parts.append(f"restarts unavailable: {_clause(holes['restarts'])}")
        elif name in restarts_15m or name in restarts_24h:
            counts = [
                f"{window[name][1]:.0f} in {label}" if name in window else f"no series in {label}"
                for label, window in (("15m", restarts_15m), ("24h", restarts_24h))
            ]
            parts.append("restarts " + ", ".join(counts))
        else:
            parts.append("restarts: no series")
        if name in created:
            parts.append(
                f"created {_stamp(created[name][1])} — creation time, not its last start"
            )
        row = f"  {scrub_addresses(name)}: " + "; ".join(parts)
        annotation = auto_name_annotation(name)
        if annotation:
            row += f" — {annotation}"
        rows.append(row)

    job = plan.entry.job_map.get(node, "") if plan.entry.job_map else ""
    header = [
        f"container_state — {describe_target(job, {})}: {len(names)} containers "
        "currently reporting.",
        f"Highest working set: {_top(working_set)}. Highest swap: {_top(swap)}. "
        "Working set includes active page cache, which is what memory pressure "
        "tracks.",
    ]
    return "\n".join([*header, *rows, *_caveats(plan)])


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
        _summary_line(summary, "ms", plan.range_window),
        *_window_lines(summary, plan.range_window),
    ]
    if baseline is not None:
        drift = summary.last - baseline
        lines.append(
            f"Measured 24h baseline for {node}: {_number(baseline)} ms — the last "
            f"reading is {_number(abs(drift))} ms "
            f"{'above' if drift >= 0 else 'below'} it."
        )
    bars = [
        f"{label} {_number(bar.value)} {bar.unit} (`{bar.source}`, "
        + (f"for {bar.for_window}" if bar.for_window else FOR_NOT_PINNED)
        + ")"
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


# --- Host coverage: shared ---------------------------------------------------


def _readable(payload: Any) -> bool:
    """A Prometheus success body whose `data.result` is a list.

    The host-coverage renderers turn an empty result into a statement (no
    cgroup moved; no unit failed), so an unreadable body must not reach that
    branch: it would read as health. :func:`_result` stays tolerant for the
    other renderers, whose empty branch is already not-derivable.
    """
    if not isinstance(payload, Mapping) or payload.get("status") != "success":
        return False
    data = payload.get("data")
    return isinstance(data, Mapping) and isinstance(data.get("result"), list)


def _range_end_lines(window: "RangeWindow | None", judged: str) -> list[str]:
    """The window's end, and what "at the window's end" is judged against.

    ``judged`` names what the result decides at the last range point, so the
    sentence says what the reader may and may not take from it.
    """
    if window is None:
        return [
            "The range window was not supplied with this plan, so no end time is "
            f"stated and {judged} is not judged."
        ]
    lines = [f"Window ends at {_stamp(window.end)}."]
    if window.end - window.last_point >= 60.0:
        lines.append(
            f"The last range point is at {_stamp(window.last_point)}: the "
            f"{window.step}s step does not divide the window evenly, so {judged} "
            "is judged at that point."
        )
    return lines


# --- memory_movers ---------------------------------------------------------

#: A host service unit's cgroup under cadvisor, `/system.slice/<unit>.service`:
#: the population D5's `id=~` selector reads.
_HOST_UNIT_ID = re.compile(r"/system\.slice/(?P<unit>[^/]+\.service)")

#: How many movers a result names. The ranking is a pointer, not the population.
MOVERS_SHOWN = 5


def _by_id(payload: Any) -> dict[str, tuple[dict[str, str], Mapping[str, Any]]]:
    """Series keyed by the cgroup `id` label ALONE.

    Not by the full label set: the instant `_over_time` results drop `__name__`
    while the range results keep it, plus the `container_label_*` labels, so a
    full-label join would match nothing (D5).
    """
    out: dict[str, tuple[dict[str, str], Mapping[str, Any]]] = {}
    for series in _result(payload):
        labels = _labels(series)
        cgroup = labels.get("id")
        if cgroup and cgroup not in out:
            out[cgroup] = (labels, series)
    return out


def _mover_name(cgroup: str, labels: Mapping[str, str]) -> tuple[str, str, str | None]:
    """(population, row label, auto-name annotation) for one cgroup."""
    name = labels.get("name")
    if name:
        return "container", f"container `{scrub_addresses(name)}`", auto_name_annotation(name)
    match = _HOST_UNIT_ID.fullmatch(cgroup)
    if match:
        return "host unit", f"host unit `{scrub_addresses(match.group('unit'))}`", None
    return "other", f"cgroup `{scrub_addresses(cgroup)}`", None


def render_memory_movers(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """The cgroups whose working set moved most, host units and containers together.

    Ranked by max − min, from the instant `max_over_time`/`min_over_time`
    answers, which are exact over every raw sample in the window. The peak's
    TIME comes from the range answer, so it is one range step coarse, and the
    result says so. A ranked cgroup with no sample at the last range point is
    flagged as gone at the window's end: the 2026-09-23 culprit ranked first and
    had no series at T (D5).
    """
    node = plan.arguments["node"]
    window_name = plan.arguments["window"]
    job = plan.entry.job_map.get(node, "") if plan.entry.job_map else ""
    window = plan.range_window

    if not (_readable(payloads.get("movers_max")) and _readable(payloads.get("movers_min"))):
        return _not_derivable(
            plan,
            f"job {job}'s working-set answer could not be read, so no ranking is "
            "given. An unreadable answer is not a ranking of nothing",
        )
    maxima = _by_id(payloads.get("movers_max"))
    minima = _by_id(payloads.get("movers_min"))
    if not maxima:
        return _not_derivable(
            plan,
            f"job {job} returned no working-set series for any host unit or named "
            f"container over {window_name}: no cgroup series were returned, which "
            "is not the same statement as 'nothing moved'",
        )
    range_readable = _readable(payloads.get("movers_series"))
    ranged = _by_id(payloads.get("movers_series")) if range_readable else {}

    movers: list[tuple[float, str, float, float, dict[str, str]]] = []
    for cgroup, (labels, series) in maxima.items():
        peak = _instant(series)
        low = _instant(minima[cgroup][1]) if cgroup in minima else None
        if peak is None or low is None:
            continue
        merged = {**(ranged[cgroup][0] if cgroup in ranged else {}), **labels}
        movers.append((peak[1] - low[1], cgroup, peak[1], low[1], merged))
    if not movers:
        return _not_derivable(
            plan,
            f"job {job} returned no cgroup with both a maximum and a minimum over "
            f"{window_name}, so no movement can be computed",
        )
    movers.sort(key=lambda item: (-item[0], item[1]))

    populations = [_mover_name(cgroup, labels)[0] for _m, cgroup, _p, _l, labels in movers]
    shown = movers[:MOVERS_SHOWN]
    rows: list[str] = []
    for rank, (moved, cgroup, peak, low, labels) in enumerate(shown, start=1):
        _population, label, annotation = _mover_name(cgroup, labels)
        parts = [f"moved by {_mib(moved)} (min {_mib(low)}, peak {_mib(peak)})"]
        points = _points(ranged[cgroup][1]) if cgroup in ranged else []
        if points:
            summary = _summarise(points)
            timing = f"peak at {_stamp(summary.max_at)}"
            if summary.max_last_at != summary.max_at:
                timing += f", last reached at {_stamp(summary.max_last_at)}"
            parts.append(timing + f"; lowest at {_stamp(summary.min_at)}")
        else:
            parts.append(
                "peak time not resolved: "
                + ("no range sample for this cgroup" if range_readable else "the range answer could not be read")
            )
        row = f"  {rank}. {label}: " + "; ".join(parts)
        if window is not None and range_readable and (
            not points or _short_of_last_point(points[-1][0], window)
        ):
            row += " — no series at the window's end (stopped or exited)"
        if annotation:
            row += f" — {annotation}"
        rows.append(row)

    def _counted(count: int, noun: str) -> str:
        return f"{count} {noun}{'' if count == 1 else 's'}"

    header = [
        f"memory_movers — {describe_target(job, {})} over {window_name}: "
        f"{_counted(len(movers), 'cgroup')} measured "
        f"({_counted(populations.count('host unit'), 'host unit')}, "
        f"{_counted(populations.count('container'), 'container')}). "
        f"Top {len(shown)} by movement (max − min over the window), UTC times:",
    ]
    resolution = (
        [
            "Peak and lowest times are the range points with the highest and "
            "lowest working set, accurate to within one range step "
            f"({window.step} s). The min and peak values are exact, from "
            "min_over_time and max_over_time over every sample in the window."
        ]
        if window is not None
        else []
    )
    return "\n".join(
        [
            *header,
            *rows,
            *resolution,
            *_range_end_lines(window, "presence at the window's end"),
            *_caveats(plan),
        ]
    )


# --- host_service_state ----------------------------------------------------


def render_host_service_state(plan: "QueryPlan", payloads: Mapping[str, Any]) -> str:
    """Units failed or activating in the window, and proof the collector ran.

    The count is checked FIRST: a collector that reported nothing makes an empty
    bad-state list meaningless, so it is not derivable rather than healthy. The
    count is units x states (node-exporter exports every unit once per state),
    so it is rendered as unit-state series and never as a unit count
    (evidence-probe 1.1).

    Each row's denominator is the request's own point count, so the fraction
    follows the configured budget (59 points over 24h at 60) rather than the
    5-minute shape of the hand investigation.
    """
    node = plan.arguments["node"]
    window_name = plan.arguments["window"]
    job = plan.entry.job_map.get(node, "") if plan.entry.job_map else ""
    window = plan.range_window

    counts = _result(payloads.get("unit_count")) if _readable(payloads.get("unit_count")) else []
    reading = _instant(counts[0]) if counts else None
    reported = reading[1] if reading is not None else 0.0
    if reported <= 0:
        return _not_derivable(
            plan,
            "the systemd collector reported no units: "
            f"count(node_systemd_unit_state) for job {job} returned "
            f"{'zero' if counts else 'no series'}, so an empty list of failed "
            "units would say nothing about this host",
        )
    if not _readable(payloads.get("bad_states")):
        return _not_derivable(
            plan,
            "the failed/activating answer could not be read, so no unit is "
            "reported either way. An unreadable answer is not a healthy host",
        )

    readings: list[tuple[str, str, list[tuple[float, float]]]] = []
    for series in _result(payloads.get("bad_states")):
        labels = _labels(series)
        unit, state = labels.get("name", ""), labels.get("state", "")
        points = [point for point in _points(series) if point[1] == 1.0]
        if unit and state and points:
            readings.append((unit, state, points))
    readings.sort(key=lambda item: (-len(item[2]), item[0], item[1]))

    expected = window.point_count if window is not None else None
    rows: list[str] = []
    for unit, state, points in readings:
        samples = len(points)
        fraction = f"{samples}/{expected}" if expected else f"{samples}"
        parts = [
            f"state {state}",
            f"{state} in {fraction} samples over {window_name}",
            f"in that state from {_stamp(points[0][0])} to {_stamp(points[-1][0])}",
        ]
        if window is None:
            parts.append(f"whether it is still {state} at the window's end is not judged")
        elif _short_of_last_point(points[-1][0], window):
            parts.append(f"not {state} at the window's end")
        else:
            parts.append(f"still {state} at the window's end")
        row = f"  `{scrub_addresses(unit)}`: " + "; ".join(parts)
        if state == "activating" and expected and samples * 2 > expected:
            row += (
                " — probable crash loop: activating in most of the window's "
                f"samples ({fraction})"
            )
        rows.append(row)

    series_count = f"{reported:.0f} unit-state series"
    if rows:
        header = [
            f"host_service_state — {describe_target(job, {})} over {window_name}, "
            f"UTC times. Units in a failed or activating state at any sample: "
            f"{len(rows)}. The collector reported {series_count} (node-exporter "
            "exports each unit once per state), so the query ran.",
        ]
    else:
        header = [
            f"host_service_state — {describe_target(job, {})} over {window_name}: "
            f"No unit was failed or activating in any sample over {window_name}. "
            f"The collector reported {series_count} (node-exporter exports each "
            "unit once per state), so the query ran and this is a reading, not an "
            "empty answer.",
        ]
    return "\n".join(
        [
            *header,
            *rows,
            *_range_end_lines(window, "whether a unit is still in its state"),
            *_caveats(plan),
        ]
    )
