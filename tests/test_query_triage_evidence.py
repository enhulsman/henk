"""Container and trend evidence for triage (triage-quality tasks 3.1-3.9).

From the `triage-quality` homelab-tools delta: "Range queries return bounded
summaries" (every figure carries its time), "Threshold comparisons are
per-resource and traceable to a measurement" (the rule's `for`; the two swap
branches), "Query parameter domains" (rp2 is not derivable, not refused),
"container_state declares both what it cannot see and what it omits", and
"container_state reports per-container memory, swap and restarts".

Fixtures are placeholders only (standing rule 1): RFC 5737 addresses
(`192.0.2.x`), invented container names, and an evaluation time that is not the
incident's. Every rendered result is swept for the fixture addresses.

**Restart counts are evaluated, not asserted into the payload.** A resets count
is computed server-side by PromQL, so a fixture that simply hands the renderer
"1" would pass whatever template the registry held. :func:`evaluate_restarts`
reads the aggregation out of the registry's own expression and applies it to raw
per-series counter samples, so the collapse to one restart per container is a
property of the template actually registered.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from henk.replay import capture
from henk.tools import query_registry
from henk.tools.homelab_query import HomelabQueryTool
from henk.tools.query_registry import (
    CADVISOR_JOBS,
    CONTAINER_NODES,
    NODE_EXPORTER_JOBS,
    QUERY_REGISTRY,
    RESTART_VERIFIED_JOBS,
    QueryOutcome,
    QueryRefused,
    Threshold,
    Unavailable,
    domain_for,
    named_container_expression,
    plan_query,
    restart_aspect_holes,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# RFC 5737 placeholders, per the change's standing rule 1.
PROMETHEUS = "http://192.0.2.10:9090"
GATUS = "http://192.0.2.11:8080"
INSTANCE = {"rp5": "192.0.2.5:8080", "vps": "192.0.2.6:8080", "rp2": "192.0.2.7:8080"}
NODE_INSTANCE = {"rp5": "192.0.2.5:9100", "vps": "192.0.2.6:9100", "rp2": "192.0.2.7:9100"}
ADGUARD_SERVER = {n: f"http://192.0.2.{i}:3000" for n, i in (("rp5", 5), ("vps", 6), ("rp2", 7))}
ADDRESSES = ("192.0.2.5", "192.0.2.6", "192.0.2.7", "192.0.2.10")

NOW = 1_756_000_000.0
STEP = 600.0
MIB = 1024 * 1024

#: The one fixed time format every summary uses.
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_T = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
_V = r"-?[\d.]+"
_EXTREME = rf"{_V}(?: at {_T}|, first reached at {_T} and last reached at {_T})"
#: The summary line both range queries share.
SUMMARY_LINE = re.compile(
    rf"Summary: .+, UTC times — first {_V} at {_T}; last {_V} at {_T}; "
    rf"min {_EXTREME}; max {_EXTREME}\. \d+ points, (?:rising|falling|flat)\. "
    rf"Window ends at {_T}\."
)

#: Words D2 removed from the swap results. The ground truth of 2026-09-23
#: (the fullness branch fired while pressure was also true) contradicts them.
FORBIDDEN_SWAP_WORDING = (
    "practically fires on pressure",
    "anti-correlated",
    "primary",
    "secondary",
)
NOT_THE_TRIGGER = "not the rule's trigger"

#: The D5 canonical table's `container_state` rows, hardcoded (never parsed from
#: design.md, which moves at archive).
D5_CONTAINER_TEMPLATES = {
    "memory_working_set": 'container_memory_working_set_bytes{job="<job>",name!=""}',
    "swap": 'container_memory_swap{job="<job>",name!=""}',
    "restarts_15m": 'max by (name) (resets(container_cpu_usage_seconds_total{job="<job>",name!=""}[15m]))',
    "restarts_24h": 'max by (name) (resets(container_cpu_usage_seconds_total{job="<job>",name!=""}[24h]))',
}


def stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def vector(*series: dict) -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": list(series)}}


def matrix(*series: dict) -> dict:
    return {"status": "success", "data": {"resultType": "matrix", "result": list(series)}}


def sample(metric: dict, value: float, *, at: float = NOW) -> dict:
    return {"metric": dict(metric), "value": [at, f"{value}"]}


def times(count: int, *, end: float = NOW) -> list[float]:
    start = end - STEP * (count - 1)
    return [start + STEP * i for i in range(count)]


def series(metric: dict, values: list[float], *, end: float = NOW) -> dict:
    return {
        "metric": dict(metric),
        "values": [[t, f"{v}"] for t, v in zip(times(len(values), end=end), values)],
    }


def window_at(window: str, *, end: float = NOW, step: float = STEP):
    """The range a request covered, as the tool hands it to the renderer."""
    span = query_registry.WINDOW_SECONDS[window]
    return query_registry.RangeWindow(start=end - span, end=end, step=int(step))


def render(query_name: str, arguments: dict, *, range_end: float | None = NOW, **payloads) -> str:
    """Render as the tool does: a range plan carries the request's own window.

    ``range_end=None`` renders a plan that was never given one, which is how a
    caller that bypasses the tool would reach the renderer.
    """
    plan = plan_query(query_name, arguments)
    if plan.range_query and plan.window is not None and range_end is not None:
        plan = dataclasses.replace(plan, range_window=window_at(plan.window, end=range_end))
    return QUERY_REGISTRY[query_name].renderer(plan, payloads)


def assert_no_addresses(text: str) -> None:
    for address in ADDRESSES:
        assert address not in text, f"{address} leaked into the rendered result"


def trend(node: str, resource: str, values: list[float], window: str = "24h") -> str:
    job = NODE_EXPORTER_JOBS[node]
    out = render(
        "node_resource_trend",
        {"node": node, "resource": resource, "window": window},
        **{resource: matrix(series({"job": job, "instance": NODE_INSTANCE[node]}, values))},
    )
    assert_no_addresses(out)
    return out


def _change_file(relative: str) -> Path:
    """A file of this change, before or after archive (which moves the folder)."""
    candidates = [REPO_ROOT / "openspec" / "changes" / "triage-quality" / relative]
    candidates += sorted(
        (REPO_ROOT / "openspec" / "changes" / "archive").glob(f"*-triage-quality/{relative}")
    )
    found = [path for path in candidates if path.is_file()]
    assert len(found) == 1, f"expected exactly one copy of {relative}, found {found}"
    return found[0]


# --- 3.1 Extremes carry their times ----------------------------------------


def test_extremes_carry_their_times():
    values = [50.0, 60.0, 90.0, 70.0, 20.0, 40.0]
    at = times(len(values))
    out = trend("rp5", "memory", values)
    assert f"first 50.00 at {stamp(at[0])}" in out
    assert f"last 40.00 at {stamp(at[5])}" in out
    assert f"max 90.00 at {stamp(at[2])}" in out
    assert f"min 20.00 at {stamp(at[4])}" in out
    assert f"Window ends at {stamp(at[5])}" in out
    # A single occurrence names one time, not a first/last pair.
    assert "first reached" not in out and "last reached" not in out


def test_a_repeated_extreme_reports_first_and_last_occurrence():
    values = [10.0, 95.5, 30.0, 95.5, 20.0, 10.0]
    at = times(len(values))
    out = trend("vps", "swap_used", values)
    assert (
        f"max 95.50, first reached at {stamp(at[1])} and last reached at {stamp(at[3])}"
        in out
    )
    # The minimum recurs too (first and last sample); it gets the same pair.
    assert (
        f"min 10.00, first reached at {stamp(at[0])} and last reached at {stamp(at[5])}"
        in out
    )


def test_summary_times_use_one_fixed_utc_format():
    out = trend("rp5", "load", [1.0, 3.0, 2.0])
    summary = next(line for line in out.splitlines() if line.startswith("Summary:"))
    tokens = re.findall(r"\d{4}-\d\d-\d\d\S*", summary)
    assert tokens, "the summary carries no time at all"
    assert all(STAMP.fullmatch(token.rstrip(".,;")) for token in tokens), tokens


def _dns(node: str, values_s: list[float]) -> str:
    out = render(
        "dns_performance",
        {"node": node, "window": "24h"},
        series=matrix(
            series({"job": "adguard-exporter", "server": ADGUARD_SERVER[node]}, values_s)
        ),
        node_mapping=vector(
            *(
                sample({"job": NODE_EXPORTER_JOBS[n], "instance": NODE_INSTANCE[n]}, 1)
                for n in ("rp5", "vps", "rp2")
            )
        ),
    )
    assert_no_addresses(out)
    return out


def test_dns_summaries_carry_the_same_times():
    values = [0.002, 0.009, 0.003, 0.009, 0.001]
    at = times(len(values))
    out = _dns("rp5", values)
    assert f"first 2.00 at {stamp(at[0])}" in out
    assert f"last 1.00 at {stamp(at[4])}" in out
    assert f"min 1.00 at {stamp(at[4])}" in out
    assert f"max 9.00, first reached at {stamp(at[1])} and last reached at {stamp(at[3])}" in out
    assert f"Window ends at {stamp(at[4])}" in out
    # The same line shape as node_resource_trend, in the same time format.
    for text in (out, trend("rp5", "load", [1.0, 2.0, 1.0])):
        line = next(line for line in text.splitlines() if line.startswith("Summary:"))
        assert SUMMARY_LINE.fullmatch(line), line


# --- 3.1 The window's end is the request's end, not the last sample --------
#
# Review-gate finding: `Window ends at` first rendered the series' last sample.
# A series that stops 20 minutes early then read as current up to the window's
# end — the node-down-mid-incident case this change exists for. The end comes
# from the request the tool sent (one clock read per invocation), carried on
# the plan, never from a clock inside the renderer.

GAP_LINE = re.compile(r"No sample for the last (.+?) of the window")


def _trend_until(last_at: float, *, range_end: float | None = NOW, window: str = "24h") -> str:
    values = [40.0, 55.0, 61.0, 58.0]
    job = NODE_EXPORTER_JOBS["rp5"]
    out = render(
        "node_resource_trend",
        {"node": "rp5", "resource": "memory", "window": window},
        range_end=range_end,
        memory=matrix(series({"job": job, "instance": NODE_INSTANCE["rp5"]}, values, end=last_at)),
    )
    assert_no_addresses(out)
    return out


def _dns_until(last_at: float, *, range_end: float | None = NOW) -> str:
    values = [0.002, 0.004, 0.003]
    out = render(
        "dns_performance",
        {"node": "rp5", "window": "24h"},
        range_end=range_end,
        series=matrix(
            series({"job": "adguard-exporter", "server": ADGUARD_SERVER["rp5"]}, values, end=last_at)
        ),
        node_mapping=vector(
            *(
                sample({"job": NODE_EXPORTER_JOBS[n], "instance": NODE_INSTANCE[n]}, 1)
                for n in ("rp5", "vps", "rp2")
            )
        ),
    )
    assert_no_addresses(out)
    return out


RANGE_RENDERERS = {"node_resource_trend": _trend_until, "dns_performance": _dns_until}


@pytest.mark.parametrize("query", sorted(RANGE_RENDERERS))
def test_the_window_end_is_the_requests_end_not_the_last_sample(query):
    last_at = NOW - 2 * STEP  # stopped 20 minutes (two steps) before the end
    out = RANGE_RENDERERS[query](last_at)
    assert f"Window ends at {stamp(NOW)}." in out
    assert f"Window ends at {stamp(last_at)}" not in out
    assert f"at {stamp(last_at)}" in out, "the last sample keeps its own time"


@pytest.mark.parametrize("query", sorted(RANGE_RENDERERS))
def test_a_series_that_stops_early_says_how_long_it_has_been_silent(query):
    last_at = NOW - 2 * STEP
    out = RANGE_RENDERERS[query](last_at)
    gaps = [line for line in out.splitlines() if GAP_LINE.search(line)]
    assert len(gaps) == 1, out
    assert GAP_LINE.search(gaps[0]).group(1) == "20 minutes"
    assert stamp(last_at) in gaps[0] and stamp(NOW) in gaps[0]


@pytest.mark.parametrize("query", sorted(RANGE_RENDERERS))
def test_the_gap_minutes_are_derived_from_the_end_and_the_last_sample(query):
    out = RANGE_RENDERERS[query](NOW - 7 * STEP)
    assert GAP_LINE.search(out).group(1) == "70 minutes"


@pytest.mark.parametrize("query", sorted(RANGE_RENDERERS))
def test_a_series_that_reaches_the_window_end_has_no_gap_line(query):
    out = RANGE_RENDERERS[query](NOW)
    assert f"Window ends at {stamp(NOW)}." in out
    assert not GAP_LINE.search(out), out


@pytest.mark.parametrize("query", sorted(RANGE_RENDERERS))
def test_a_plan_without_its_window_states_no_end_rather_than_guessing(query):
    """No request window on the plan: say so. Never the last sample, never a clock."""
    last_at = NOW - 2 * STEP
    out = RANGE_RENDERERS[query](last_at, range_end=None)
    assert "Window ends at" not in out
    assert "window's end time was not supplied" in out
    assert not GAP_LINE.search(out)


def test_an_uneven_step_names_the_last_range_point_and_is_not_a_gap():
    """Prometheus evaluates at start + k*step <= end. With max_points=60 a 24h
    window's step (1465 s) leaves the last point 1430 s before `end`, so a
    series current to that point is not silent — and says where the grid ends."""
    rw = query_registry.range_window("24h", NOW, 60)
    assert (rw.step, rw.end - rw.last_point) == (1465, 1430)
    plan = dataclasses.replace(
        plan_query("node_resource_trend", {"node": "rp5", "resource": "memory", "window": "24h"}),
        range_window=rw,
    )
    points = [[rw.last_point - rw.step * i, "50"] for i in (2, 1, 0)]
    out = plan.entry.renderer(plan, {"memory": matrix({"metric": {"job": "node-exporter-pi5"}, "values": points})})
    assert f"Window ends at {stamp(NOW)}." in out
    assert not GAP_LINE.search(out), out
    assert f"The last range point is at {stamp(rw.last_point)}" in out
    # One grid point missing at the end is a gap, measured to the window's end.
    out = plan.entry.renderer(
        plan, {"memory": matrix({"metric": {"job": "node-exporter-pi5"}, "values": points[:-1]})}
    )
    assert GAP_LINE.search(out).group(1) == "48 minutes"  # (1430 + 1465) s


def _advancing_clock(start: float = NOW, tick: float = 37.0):
    reads: list[float] = []

    def clock() -> float:
        reads.append(start + tick * len(reads))
        return reads[-1]

    return clock, reads


@pytest.mark.parametrize("query", sorted(RANGE_RENDERERS))
async def test_the_tool_reads_the_clock_once_and_renders_the_end_it_sent(query):
    clock, reads = _advancing_clock()
    sent: list[dict] = []
    last_at = NOW - 2 * STEP

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.url.path.endswith("/query_range"):
            sent.append(params)
            metric = (
                {"job": "adguard-exporter", "server": ADGUARD_SERVER["rp5"]}
                if query == "dns_performance"
                else {"job": NODE_EXPORTER_JOBS["rp5"]}
            )
            return httpx.Response(200, json=matrix(series(metric, [0.002, 0.003], end=last_at)))
        return httpx.Response(
            200,
            json=vector(
                *(
                    sample({"job": NODE_EXPORTER_JOBS[n], "instance": NODE_INSTANCE[n]}, 1)
                    for n in ("rp5", "vps", "rp2")
                )
            ),
        )

    tool = HomelabQueryTool(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        gatus_url=GATUS,
        prometheus_url=PROMETHEUS,
        max_points=60,
        clock=clock,
    )
    arguments = {"node": "rp5", "window": "1h"}
    if query == "node_resource_trend":
        arguments["resource"] = "memory"
    result = await tool.run(query_name=query, **arguments)
    assert result.ok is True, result.error
    assert len(reads) == 1, f"the clock was read {len(reads)} times in one invocation"
    assert [float(p["end"]) for p in sent] == [reads[0]]
    assert f"Window ends at {stamp(reads[0])}." in result.content
    assert GAP_LINE.search(result.content), result.content


# --- 3.2 The rule's `for` window -------------------------------------------


def test_a_comparison_states_the_rules_for_window():
    swap_io = trend("vps", "swap_io", [12.0, 83.42])
    assert "50.00 pages/s" in swap_io and "`HenkSwapPressure`" in swap_io
    assert "holds for 15m" in swap_io

    memory = trend("rp5", "memory", [52.0, 57.41])
    assert "75.00 percent used" in memory and "`High memory usage`" in memory
    assert "holds for 5m" in memory

    disk = trend("vps", "disk", [30.0, 12.5])
    assert "`HenkDiskPressure`" in disk and "holds for 15m" in disk


def test_a_threshold_with_no_recorded_for_says_so_and_invents_none():
    plan = plan_query("node_resource_trend", {"node": "rp5", "resource": "memory", "window": "1h"})
    entry = plan.entry
    bare = dataclasses.replace(entry.thresholds["memory"], for_window=None)
    plan = dataclasses.replace(
        plan, entry=dataclasses.replace(entry, thresholds={**entry.thresholds, "memory": bare})
    )
    payload = matrix(series({"job": "node-exporter-pi5"}, [52.0, 57.41]))
    out = entry.renderer(plan, {"memory": payload})
    assert "the rule's `for` is not in the pinned record" in out
    assert "holds for" not in out
    assert "5m" not in out


def test_every_threshold_declares_its_for_window_field():
    fields = {f.name for f in dataclasses.fields(Threshold)}
    assert "for_window" in fields and "branch" in fields
    assert "is_trigger" not in fields, "D2 replaces is_trigger with branch"


def test_dns_bars_carry_their_rules_for_windows():
    out = _dns("rp5", [0.002, 0.003])
    assert "`Pi5HighDNSProcessingTime`, for 5m" in out
    assert "`Pi5CriticalDNSProcessingTime`, for 2m" in out
    assert "`DNSProcessingTimeCritical`, for 10m" in out


# --- 3.3 The two swap branches ---------------------------------------------

BOTH_BRANCHES = (
    "`HenkSwapPressure` fires on either branch, and the alert's value does not say "
    "which fired; check both `swap_used` and `swap_io`."
)
CROSSED = "this branch alone can fire `HenkSwapPressure` once sustained for 15m."
CLEAR = "below this branch's bar; on its own this branch would not fire the rule."


def test_swap_fullness_below_its_bar_is_not_presented_as_an_approaching_incident():
    # 84 % full on the vps: below the 95 % bar, inside the documented chronic range.
    out = trend("vps", "swap_used", [77.0, 84.0])
    assert "fullness branch" in out
    assert CLEAR in out
    assert CROSSED not in out
    assert "approaching" not in out.lower()


def test_swap_fullness_above_its_bar_is_presented_as_a_branch_that_can_fire():
    out = trend("vps", "swap_used", [90.0, 97.0, 96.5])
    assert "fullness branch" in out
    assert CROSSED in out
    assert CLEAR not in out
    assert NOT_THE_TRIGGER not in out.lower()


@pytest.mark.parametrize("values", [[12.0, 83.42], [3.0, 4.0]])
def test_swap_pressure_names_its_branch_both_ways(values):
    out = trend("vps", "swap_io", values)
    assert "pressure branch" in out
    crossed = max(values) > 50
    assert (CROSSED in out) is crossed
    assert (CLEAR in out) is not crossed


@pytest.mark.parametrize("resource", ["swap_used", "swap_io"])
# [3.0, 4.0] keeps swap_io clear of its 50 pages/s bar, so the sweep covers the
# pressure branch's clear-bar wording as well as its crossed one.
@pytest.mark.parametrize("values", [[77.0, 84.0], [96.0, 99.0], [1.0, 120.0], [3.0, 4.0]])
def test_swap_results_say_both_branches_need_checking(resource, values):
    out = trend("vps", resource, values)
    assert BOTH_BRANCHES in out
    lowered = out.lower()
    for phrase in FORBIDDEN_SWAP_WORDING:
        assert phrase not in lowered, phrase
    assert NOT_THE_TRIGGER not in lowered


def test_non_swap_results_carry_no_branch_line():
    for resource, values in (("memory", [50.0, 80.0]), ("disk", [30.0, 12.0])):
        out = trend("rp5", resource, values)
        assert "either branch" not in out
        assert "branch" not in out


def test_the_old_trigger_wording_appears_in_no_rendered_result():
    """"not the rule's trigger" appears in no rendered result of any query."""
    outputs = [trend("vps", r, [10.0, 20.0]) for r in ("cpu", "memory", "disk", "load", "swap_used", "swap_io")]
    outputs.append(_dns("vps", [0.002, 0.003]))
    outputs.append(render("container_state", {"node": "vps"}, **_container_payloads("vps")))
    for out in outputs:
        assert NOT_THE_TRIGGER not in out.lower()
    # And no registry string that could be rendered carries it either.
    for entry in QUERY_REGISTRY.values():
        blobs = list(entry.caveats) + [t.note for t in entry.thresholds.values()]
        blobs += [u.reason for u in entry.unavailable]
        for blob in blobs:
            lowered = blob.lower()
            assert NOT_THE_TRIGGER not in lowered, entry.name
            for phrase in FORBIDDEN_SWAP_WORDING:
                assert phrase not in lowered, (entry.name, phrase)


# --- 3.4 Per-container memory and swap -------------------------------------

#: name -> (working set bytes, swap bytes). `quirky_turing` is the auto-name shape.
CONTAINERS = {
    "example-a": (900 * MIB, 10 * MIB),
    "example-b": (300 * MIB, 200 * MIB),
    "example-c": (650 * MIB, 0),
    "quirky_turing": (100 * MIB, 50 * MIB),
}


def _container_payloads(node: str, *, health: bool = True, containers=CONTAINERS) -> dict:
    job = CADVISOR_JOBS[node]

    def labels(name: str, **extra: str) -> dict:
        return {"job": job, "instance": INSTANCE[node], "name": name, "id": f"/docker/{name}", **extra}

    def each(value) -> dict:
        return vector(*(sample(labels(n), value(n, v)) for n, v in containers.items()))

    payloads = {
        "last_seen": each(lambda n, v: NOW - 10.0),
        "created": each(lambda n, v: NOW - 400_000.0),
        "oom_events": each(lambda n, v: 0),
        "memory_working_set": each(lambda n, v: v[0]),
        "swap": each(lambda n, v: v[1]),
        "restarts_15m": vector(*(sample({"name": n}, 0) for n in containers)),
        "restarts_24h": vector(*(sample({"name": n}, 0) for n in containers)),
        "health_state": each(lambda n, v: 1) if health else vector(),
    }
    return payloads


def _row(out: str, name: str) -> str:
    rows = [line for line in out.splitlines() if line.startswith(f"  {name}:")]
    assert len(rows) == 1, (name, out)
    return rows[0]


@pytest.mark.parametrize("node", ["rp5", "vps"])
def test_memory_and_swap_are_reported_per_container(node):
    out = render("container_state", {"node": node}, **_container_payloads(node))
    assert_no_addresses(out)
    for name, (working_set, swap) in CONTAINERS.items():
        row = _row(out, name)
        assert f"working set {working_set / MIB:.1f} MiB" in row
        assert f"swap {swap / MIB:.1f} MiB" in row
    assert (
        "Highest working set: example-a (900.0 MiB), example-c (650.0 MiB), "
        "example-b (300.0 MiB)." in out
    )
    assert (
        "Highest swap: example-b (200.0 MiB), quirky_turing (50.0 MiB), "
        "example-a (10.0 MiB)." in out
    )
    assert "includes active page cache, which is what memory pressure tracks" in out


def test_container_memory_carries_no_bar():
    out = render("container_state", {"node": "vps"}, **_container_payloads("vps"))
    assert QUERY_REGISTRY["container_state"].thresholds == {}
    assert "Compared against" not in out
    assert "no alert rule defines one" in out


def test_an_auto_generated_name_is_annotated_and_hedged():
    out = render("container_state", {"node": "vps"}, **_container_payloads("vps"))
    row = _row(out, "quirky_turing")
    assert "looks auto-generated" in row
    assert "probably an ephemeral `docker run` container" in row
    assert "a hand-chosen name can share this form" in row, "the annotation is hedged"
    for name in ("example-a", "example-b", "example-c"):
        assert "auto-generated" not in _row(out, name)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("suspicious_mendeleev", True),
        ("quirky_turing4", True),
        ("my_app", True),  # hand-chosen, same shape: why the annotation is hedged
        ("taiga-docker-taiga-front-1", False),
        ("gatus", False),
        ("Upper_Case", False),
        ("three_part_name", False),
        ("_leading", False),
    ],
)
def test_the_auto_name_shape_is_dockers_adjective_surname(name, expected):
    from henk.tools.query_renderers import auto_name_annotation

    assert (auto_name_annotation(name) is not None) is expected


def test_host_units_are_pointed_elsewhere_not_silently_missing():
    out = render("container_state", {"node": "rp5"}, **_container_payloads("rp5", health=False))
    assert "Host systemd units are not containers and are not listed here" in out
    assert "`memory_movers` covers them" in out


# --- 3.5 rp2: not derivable, not refused -----------------------------------


def _refusing_transport(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"an rp2 container query issued a request to {request.url}")


def _tool(handler=None) -> HomelabQueryTool:
    return HomelabQueryTool(
        httpx.AsyncClient(transport=httpx.MockTransport(handler or _refusing_transport)),
        gatus_url=GATUS,
        prometheus_url=PROMETHEUS,
        max_points=60,
        clock=lambda: NOW,
    )


async def test_rp2_container_state_is_not_available_and_says_so():
    result = await _tool().run(query_name="container_state", node="rp2")
    assert result.ok is True, "not derivable is a result, not a refusal"
    message = result.content
    assert "rp2 runs no cadvisor" in message
    assert "no container metrics" in message
    assert "outside" not in message, "this is not an out-of-domain refusal"
    assert "0 containers" not in message and "containers currently reporting" not in message
    plan = plan_query("container_state", {"node": "rp2"})
    assert plan.outcome is QueryOutcome.NOT_DERIVABLE
    assert plan.expressions == {}


def test_a_named_container_follow_up_on_rp2_is_not_derivable():
    with pytest.raises(QueryRefused) as exc:
        named_container_expression("rp2", "example-a", known=("example-a",))
    assert exc.value.outcome is QueryOutcome.NOT_DERIVABLE
    assert "rp2 runs no cadvisor" in str(exc.value)
    assert "outside" not in str(exc.value)
    # Same outcome and statement as the query itself.
    assert str(exc.value) == plan_query("container_state", {"node": "rp2"}).message


def test_a_named_container_follow_up_outside_the_domain_is_still_refused():
    with pytest.raises(QueryRefused) as exc:
        named_container_expression("nas", "example-a", known=("example-a",))
    assert exc.value.outcome is QueryOutcome.OUT_OF_DOMAIN


async def test_an_out_of_domain_value_is_rejected_not_answered_emptily():
    result = await _tool().run(
        query_name="node_resource_trend", node="nas", resource="cpu", window="1h"
    )
    assert result.ok is False
    assert "nas" in result.error and "outside this query's domain" in result.error


def test_container_state_node_domain_is_its_own_tuple():
    domain = domain_for("container_state", "node")
    assert domain is CONTAINER_NODES
    assert domain == ("rp5", "vps", "rp2")
    assert domain != tuple(CADVISOR_JOBS)
    assert set(domain) - set(CADVISOR_JOBS) == {"rp2"}
    # CADVISOR_JOBS stays the job map, and rp2 has no job in it.
    assert QUERY_REGISTRY["container_state"].job_map is CADVISOR_JOBS
    assert "rp2" not in CADVISOR_JOBS
    assert any(
        u.parameters == {"node": "rp2"} and u.aspect is None
        for u in QUERY_REGISTRY["container_state"].unavailable
    )


# --- 3.6 Restarts ----------------------------------------------------------

_RESETS = re.compile(
    r"^(?:(?P<agg>\w+) by \((?P<by>\w+)\) \()?resets\("
    r'container_cpu_usage_seconds_total\{job="(?P<job>[^"]+)",name!=""\}'
    r"\[(?P<window>\w+)\]\)\)?$"
)
_WINDOW_S = {"15m": 900, "24h": 86400}


def evaluate_restarts(expression: str, raw: list[dict], at: float) -> dict:
    """A PromQL-faithful evaluation of the registry's restart expression.

    ``raw`` holds counter series as ``{"metric": labels, "samples": [(t, v)]}``.
    `resets(v[w])` counts decreases between consecutive samples in (at-w, at];
    the optional outer `<agg> by (<label>)` is then applied as written.
    """
    match = _RESETS.match(expression)
    assert match, f"not a restart expression this evaluator understands: {expression!r}"
    span = _WINDOW_S[match["window"]]
    per_series = []
    for item in raw:
        if item["metric"].get("job") != match["job"] or not item["metric"].get("name"):
            continue
        points = [v for t, v in item["samples"] if at - span < t <= at]
        count = sum(1 for a, b in zip(points, points[1:]) if b < a)
        per_series.append((item["metric"], float(count)))
    if match["agg"] is None:
        return vector(*(sample(m, c, at=at) for m, c in per_series))
    reducers = {"max": max, "min": min, "sum": sum}
    groups: dict[str, list[float]] = {}
    for metric, count in per_series:
        groups.setdefault(metric[match["by"]], []).append(count)
    return vector(
        *(
            sample({match["by"]: key}, reducers[match["agg"]](counts), at=at)
            for key, counts in sorted(groups.items())
        )
    )


def _counter(node: str, name: str, values: list[float], *, cpu: str = "total") -> dict:
    return {
        "metric": {"job": CADVISOR_JOBS[node], "instance": INSTANCE[node], "name": name, "cpu": cpu},
        # One sample every 30 s, the measured scrape interval, ending at NOW.
        "samples": [(NOW - 30.0 * (len(values) - 1 - i), v) for i, v in enumerate(values)],
    }


def _restart_router(node: str, raw: list[dict], payloads: dict):
    plan = plan_query("container_state", {"node": node})
    by_expression = {plan.expressions[role]: role for role in plan.expressions}

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        role = by_expression.get(query)
        assert role is not None, f"unplanned request {query!r}"
        if role.startswith("restarts_"):
            return httpx.Response(200, json=evaluate_restarts(query, raw, NOW))
        return httpx.Response(200, json=payloads[role])

    return handler


@pytest.mark.parametrize("node", ["rp5", "vps"])
async def test_a_restart_is_counted(node):
    # A `docker restart` 5 minutes ago: the counter falls from ~2014 s to 1.9 s,
    # the container keeps its series, and its creation time does not move.
    before = [2000.0 + i for i in range(15)]
    after = [1.9 + i for i in range(10)]
    # example-c restarted about two hours ago: inside the 24h lookback, outside
    # the 15m one, so the two columns must differ (300 samples at 30 s = 2.5 h).
    earlier = [500.0 + i for i in range(60)] + [0.4 + i for i in range(240)]
    raw = [
        _counter(node, "example-a", before + after),
        _counter(node, "example-b", [100.0 + i for i in range(25)]),
        _counter(node, "example-c", earlier),
    ]
    created = NOW - 400_000.0
    names = ("example-a", "example-b", "example-c")
    payloads = _container_payloads(node, containers={n: (1, 1) for n in names})
    payloads["created"] = vector(
        *(sample({"job": CADVISOR_JOBS[node], "name": n}, created) for n in names)
    )
    result = await _tool(_restart_router(node, raw, payloads)).run(
        query_name="container_state", node=node
    )
    assert result.ok is True, result.error
    row = _row(result.content, "example-a")
    assert "restarts 1 in 15m, 1 in 24h" in row
    assert f"created {stamp(created)}" in row, "the creation time did not move"
    assert "restarts 0 in 15m, 0 in 24h" in _row(result.content, "example-b")
    assert "restarts 0 in 15m, 1 in 24h" in _row(result.content, "example-c")
    assert_no_addresses(result.content)


def test_per_cpu_series_count_one_restart_once():
    raw = [
        _counter("vps", "example-a", [500.0, 501.0, 0.5, 1.5], cpu=f"cpu{i}") for i in range(4)
    ]
    plan = plan_query("container_state", {"node": "vps"})
    # The fixture is discriminating: without the collapse it would count four.
    summed = evaluate_restarts(plan.expressions["restarts_15m"].replace("max by", "sum by"), raw, NOW)
    assert summed["data"]["result"][0]["value"][1] == "4.0"
    payloads = _container_payloads("vps", containers={"example-a": (1, 1)})
    payloads["restarts_15m"] = evaluate_restarts(plan.expressions["restarts_15m"], raw, NOW)
    payloads["restarts_24h"] = evaluate_restarts(plan.expressions["restarts_24h"], raw, NOW)
    out = QUERY_REGISTRY["container_state"].renderer(plan, payloads)
    assert "restarts 1 in 15m, 1 in 24h" in _row(out, "example-a")


def test_the_restart_template_collapses_per_container_name():
    for role in ("restarts_15m", "restarts_24h"):
        expression = QUERY_REGISTRY["container_state"].expressions[role]
        assert expression.startswith("max by (name) (resets(")


@pytest.mark.parametrize("node", ["rp5", "vps"])
def test_the_restart_caveat_describes_the_measured_behaviour(node):
    out = render("container_state", {"node": node}, **_container_payloads(node, health=node == "vps"))
    lowered = out.lower()
    assert "restarts are counted as resets of the container's cpu counter" in lowered
    assert "a recreate (`compose up`, a new image) starts a new series" in lowered
    assert "only as a new creation time" in lowered
    assert "several restarts inside one scrape interval count as one" in lowered
    assert "creation time" in lowered and "does not move on a restart" in lowered
    assert "not observable" not in lowered


# --- 3.7 An unavailable aspect is rendered in its place ---------------------


def test_an_unavailable_aspect_is_rendered_in_its_place():
    out = render("container_state", {"node": "rp5"}, **_container_payloads("rp5", health=False))
    reason = next(
        u.reason
        for u in QUERY_REGISTRY["container_state"].unavailable
        if u.parameters == {"node": "rp5"} and u.aspect == "health_state"
    )
    for name in CONTAINERS:
        row = _row(out, name)
        assert f"health state unavailable: {reason.rstrip('.')}" in row, row
        assert "health state 0" not in row
        assert "health state: no series" not in row
    # Where there is no hole, a missing reading is stated as such, not as the hole.
    vps = render("container_state", {"node": "vps"}, **_container_payloads("vps", health=False))
    assert "health state: no series for this container" in _row(vps, "example-a")
    assert reason not in vps


def test_every_aspect_hole_is_read_from_the_plan(monkeypatch):
    """The mechanism is generic: a restarts hole renders its reason in that column."""
    entry = QUERY_REGISTRY["container_state"]
    hole = Unavailable(parameters={"node": "vps"}, aspect="restarts", reason="placeholder reason X.")
    monkeypatch.setitem(
        query_registry._REGISTRY,
        "container_state",
        dataclasses.replace(entry, unavailable=entry.unavailable + (hole,)),
    )
    plan = plan_query("container_state", {"node": "vps"})
    assert plan.unavailable_aspects == {"restarts": "placeholder reason X."}
    payloads = _container_payloads("vps")
    payloads["restarts_15m"] = vector()
    payloads["restarts_24h"] = vector()
    out = QUERY_REGISTRY["container_state"].renderer(plan, payloads)
    for name in CONTAINERS:
        row = _row(out, name)
        assert "restarts unavailable: placeholder reason X" in row
        assert "0 in 15m" not in row


# --- 3.8 The restart aspect is traceable to a measurement -------------------

_VERDICT = re.compile(r"^restart-signal (\S+): (\w+)$", re.MULTILINE)


def recorded_restart_verdicts() -> dict[str, str]:
    text = _change_file("notes/evidence-probe.md").read_text()
    found = _VERDICT.findall(text)
    verdicts = dict(found)
    # Coverage: one line per cadvisor job, each exactly once. A parser that
    # matches nothing would make the comparison below a green no-op.
    assert len(found) == len(verdicts), "a verdict line is repeated"
    assert set(verdicts) == set(CADVISOR_JOBS.values()), verdicts
    return verdicts


def test_the_restart_aspect_is_traceable_to_a_measurement():
    verified = {job for job, verdict in recorded_restart_verdicts().items() if verdict == "verified"}
    assert set(RESTART_VERIFIED_JOBS) == verified
    for node, job in CADVISOR_JOBS.items():
        plan = plan_query("container_state", {"node": node})
        has_aspect = "restarts" not in plan.unavailable_aspects
        assert has_aspect is (job in verified), node
        assert {"restarts_15m", "restarts_24h"} <= set(plan.expressions)


def test_an_unverified_job_would_get_a_restart_hole():
    holes = restart_aspect_holes(frozenset({"cadvisor-pi5"}))
    assert [(dict(h.parameters), h.aspect) for h in holes] == [({"node": "vps"}, "restarts")]
    assert "not verified" in holes[0].reason
    assert restart_aspect_holes(frozenset(CADVISOR_JOBS.values())) == ()


# --- 3.9 The container templates are the D5 contract -----------------------


def test_the_container_templates_are_byte_equal_to_the_d5_literals():
    entry = QUERY_REGISTRY["container_state"]
    for role, literal in D5_CONTAINER_TEMPLATES.items():
        assert entry.expressions[role] == literal, role
    # container_state is instant-only: every D5 row for it is `instant`.
    assert entry.range_query is False


def test_the_capture_no_longer_writes_out_the_container_rows():
    # Task 4.4 retired the whole written-out table (group 3 retired these four
    # rows, group 4 the remaining five), so no copy of any row survives.
    assert not hasattr(capture, "WRITTEN_OUT_TEMPLATES")
    # The capture now sends exactly the registry's own container templates.
    requests = [r for r in capture.plan_requests() if r.query == "container_state"]
    assert {r.source for r in requests} == {"registry"}
    for node in ("rp5", "vps"):
        sent = {r.role: r.expression for r in requests if dict(r.arguments)["node"] == node}
        for role, literal in D5_CONTAINER_TEMPLATES.items():
            assert sent[role] == literal.replace("<job>", CADVISOR_JOBS[node])
    assert not [r for r in requests if dict(r.arguments)["node"] == "rp2"]


def test_every_container_template_stays_address_free():
    for template in QUERY_REGISTRY["container_state"].templates.values():
        assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b|://", template)
        assert "instance" not in template
