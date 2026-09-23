"""Host-coverage queries: `memory_movers` and `host_service_state` (triage-quality 4.1-4.4).

From the `triage-quality` homelab-tools delta: "Named-query tool with a closed
query-name enum" (*The host-coverage queries are enumerated*), "No rule-state
query in v1", "Query parameter domains", "memory_movers names the cgroups whose
working set moved, containers and host units alike", and "host_service_state
reports failed and activating host units, and proves it ran".

Fixtures are placeholders only (standing rule 1): RFC 5737 addresses
(`192.0.2.x`), invented unit names (`example-a.service`) and container names, and
an evaluation time that is not the incident's. Every rendered result is swept
for the fixture addresses.

**Range fixtures sit on the request's own grid.** Prometheus evaluates a range at
``start + k*step`` while that is ``<= end``, and with 60 points the step divides
no supported window (the 24h step of 1465 s leaves the last point 1430 s before
``end``). The fixtures therefore place samples on :class:`RangeWindow` grid
points, and "present at the window's end" means "has a sample at the last range
point", judged by the same half-step rule group 3 uses for a silent trend.

**Denominators come from the fixture's step, never from a literal 288.** The
2026-09-23 hand investigation read 288 points at a 5-minute step. rp5's point
budget of 60 gives 59 points over 24h (`notes/evidence-probe.md`, 1.1), so the
expected-count literals below are pinned per budget: 59 at 60 points and 289 at
the 300 s step a 289-point budget gives (a range over 24h at 300 s has a point
at both ends).

**The D5 templates are literals in this file.** They are never parsed from
`design.md`, which moves at archive. In the D5 table the `bad_states` row reads
`activating\\|failed`; the `\\|` is Markdown escaping and the template's text is
`activating|failed`.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from henk.config import QUERY_TOOL_SUMMARIES, build_system_prompt
from henk.replay import capture
from henk.tools.homelab_query import HomelabQueryTool, build_parameters_schema
from henk.tools.query_registry import (
    CADVISOR_JOBS,
    CONTAINER_NODES,
    HOST_SERVICE_NODES,
    MOVERS_NODES,
    NODE_EXPORTER_JOBS,
    PROMETHEUS_WINDOWS,
    QUERY_NAMES,
    QUERY_REGISTRY,
    QueryBackend,
    QueryOutcome,
    QueryRefused,
    RangeWindow,
    domain_for,
    plan_query,
    range_window,
    with_range_end,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# RFC 5737 placeholders, per the change's standing rule 1.
PROMETHEUS = "http://192.0.2.10:9090"
GATUS = "http://192.0.2.11:8080"
CADVISOR_INSTANCE = "192.0.2.6:8080"
NODE_INSTANCE = "192.0.2.6:9100"
ADDRESSES = ("192.0.2.6", "192.0.2.9", "192.0.2.10")

NOW = 1_756_000_000.0
MIB = 1024 * 1024
VPS_CADVISOR = CADVISOR_JOBS["vps"]
VPS_NODE_EXPORTER = NODE_EXPORTER_JOBS["vps"]

#: The D5 canonical table's `memory_movers` and `host_service_state` rows,
#: hardcoded (never parsed from design.md). Kind, then the template.
D5_HOST_COVERAGE_TEMPLATES = {
    ("memory_movers", "movers_max"): (
        "instant",
        r'max_over_time(container_memory_working_set_bytes{job="<job>",id=~"/system\\.slice/.+\\.service"}[<window>])'
        r' or max_over_time(container_memory_working_set_bytes{job="<job>",name!=""}[<window>])',
    ),
    ("memory_movers", "movers_min"): (
        "instant",
        r'min_over_time(container_memory_working_set_bytes{job="<job>",id=~"/system\\.slice/.+\\.service"}[<window>])'
        r' or min_over_time(container_memory_working_set_bytes{job="<job>",name!=""}[<window>])',
    ),
    ("memory_movers", "movers_series"): (
        "range",
        r'container_memory_working_set_bytes{job="<job>",id=~"/system\\.slice/.+\\.service"}'
        r' or container_memory_working_set_bytes{job="<job>",name!=""}',
    ),
    ("host_service_state", "bad_states"): (
        "range",
        'node_systemd_unit_state{job="<job>",state=~"activating|failed"} == 1',
    ),
    ("host_service_state", "unit_count"): (
        "instant",
        'count(node_systemd_unit_state{job="<job>"})',
    ),
}

#: Expected points over 24h, per point budget: pinned literals, not recomputed.
EXPECTED_24H_POINTS = {60: 59, 289: 289}

#: The advertised enum, from the triage-quality delta's closed list.
_TIMESTAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"


def stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def vector(*series: dict) -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": list(series)}}


def matrix(*series: dict) -> dict:
    return {"status": "success", "data": {"resultType": "matrix", "result": list(series)}}


def assert_no_addresses(text: str) -> None:
    for address in ADDRESSES:
        assert address not in text, f"{address} leaked into the rendered result"


def grid(window: RangeWindow) -> list[float]:
    """Every evaluation time of a range request, ``start`` to its last point."""
    count = int((window.end - window.start) // window.step) + 1
    return [window.start + k * window.step for k in range(count)]


def planned(query: str, arguments: dict, *, max_points: int = 60, end: float = NOW):
    """The plan exactly as the tool hands it to the renderer."""
    return with_range_end(plan_query(query, arguments), end, max_points)


def render_plan(plan, **payloads) -> str:
    out = plan.entry.renderer(plan, payloads)
    assert_no_addresses(out)
    return out


def _change_file(relative: str) -> Path:
    candidates = [REPO_ROOT / "openspec" / "changes" / "triage-quality" / relative]
    candidates += sorted(
        (REPO_ROOT / "openspec" / "changes" / "archive").glob(f"*-triage-quality/{relative}")
    )
    found = [path for path in candidates if path.is_file()]
    assert len(found) == 1, f"expected exactly one copy of {relative}, found {found}"
    return found[0]


def spec_enum() -> set[str]:
    """The delta's closed enum: the bullet list after "The enum SHALL be exactly"."""
    text = _change_file("specs/homelab-tools/spec.md").read_text()
    found = re.search(r"The enum SHALL be\s+exactly:\n", text)
    assert found, "the spec's enum sentence moved"
    names: list[str] = []
    for line in text[found.end():].splitlines():
        match = re.fullmatch(r"- `(\w+)`", line.strip())
        if not match:
            break
        names.append(match.group(1))
    assert len(names) >= 6, "the spec's enum list was not parsed"
    return set(names)


def _refusing_transport(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"a not-derivable or refused query issued a request to {request.url}")


class Recorder:
    def __init__(self, answer=None):
        self.requests: list[httpx.Request] = []
        self._answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._answer is not None:
            return httpx.Response(200, json=self._answer(request))
        kind = "matrix" if request.url.path.endswith("query_range") else "vector"
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": kind, "result": []}}
        )


def _tool(handler=None, *, max_points: int = 60) -> HomelabQueryTool:
    return HomelabQueryTool(
        httpx.AsyncClient(transport=httpx.MockTransport(handler or _refusing_transport)),
        gatus_url=GATUS,
        prometheus_url=PROMETHEUS,
        max_points=max_points,
        clock=lambda: NOW,
    )


# =============================================================================
# 4.1 The enum, no rule-state query, the domains
# =============================================================================


def test_the_host_coverage_queries_are_enumerated():
    names = spec_enum()
    assert {"memory_movers", "host_service_state"} <= names
    assert {"memory_movers", "host_service_state"} <= set(QUERY_NAMES)
    # The advertised enum equals the dispatch table, and both equal the spec.
    assert set(QUERY_NAMES) == set(QUERY_REGISTRY) == names
    assert build_parameters_schema()["properties"]["query_name"]["enum"] == list(QUERY_NAMES)


def test_no_rule_state_query_is_registered():
    forbidden = re.compile(r"ALERTS|GRAFANA_ALERTS|alertstate|/rules|/alerts|/api/v1/alerts")
    for entry in QUERY_REGISTRY.values():
        for role, template in entry.templates.items():
            assert not forbidden.search(template), f"{entry.name}.{role} reads rule state"
        if entry.backend is QueryBackend.GATUS:
            # A check history: Gatus's own endpoint results.
            assert all(r.startswith("/api/v1/endpoints/") for r in entry.routes.values())
        else:
            # A measured series, or Prometheus's own scrape state.
            assert set(entry.routes.values()) <= {"/api/v1/targets"}
            assert entry.expressions, entry.name
    assert not any("alert" in name or "rule" in name for name in QUERY_NAMES)
    # The two new ones read node-exporter and cadvisor series, nothing else.
    for name in ("memory_movers", "host_service_state"):
        for template in QUERY_REGISTRY[name].expressions.values():
            assert re.search(r"container_memory_working_set_bytes|node_systemd_unit_state", template)


_SNAKE = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")


def _registry_prose():
    for entry in QUERY_REGISTRY.values():
        yield entry.name, entry.summary
        for caveat in entry.caveats:
            yield entry.name, caveat
        for hole in entry.unavailable:
            yield entry.name, hole.reason
        for parameter in entry.parameters:
            yield entry.name, parameter.note


def test_every_backticked_query_name_in_a_caveat_is_registered():
    """A caveat that points at a query must point at one that exists.

    `container_state`'s caveat sends the reader to `memory_movers`; before group
    4 that name dangled. A backticked snake_case identifier in registry prose is
    either a registered query or a metric one of the registry's templates reads.
    """
    metrics = " ".join(t for e in QUERY_REGISTRY.values() for t in e.templates.values())
    referenced = set()
    for owner, text in _registry_prose():
        for token in _SNAKE.findall(text):
            if token in QUERY_REGISTRY:
                referenced.add(token)
                continue
            assert token in metrics, (
                f"{owner} prose names `{token}`, which is neither a registered "
                "query nor a metric any template reads"
            )
    # Non-vacuous: the pointer this test exists for is among them.
    assert "memory_movers" in referenced


@pytest.mark.parametrize("query", ["memory_movers", "host_service_state"])
def test_the_host_coverage_domains_are_their_own_tuples(query):
    assert domain_for(query, "node") == ("rp5", "vps", "rp2")
    assert tuple(domain_for(query, "window")) == PROMETHEUS_WINDOWS
    assert {p.name for p in QUERY_REGISTRY[query].parameters} == {"node", "window"}
    # Its own declared tuple, never the job map. (Identity against
    # `CONTAINER_NODES` would prove nothing: CPython shares equal tuple
    # constants within a module.)
    own = {"memory_movers": MOVERS_NODES, "host_service_state": HOST_SERVICE_NODES}[query]
    assert domain_for(query, "node") is own
    assert set(domain_for(query, "node")) != set(CADVISOR_JOBS)
    assert set(domain_for(query, "node")) == set(CONTAINER_NODES)


def test_the_host_coverage_job_maps():
    assert dict(QUERY_REGISTRY["memory_movers"].job_map) == dict(CADVISOR_JOBS)
    assert dict(QUERY_REGISTRY["host_service_state"].job_map) == dict(NODE_EXPORTER_JOBS)


async def test_rp2_memory_movers_is_not_available_and_says_so():
    """*rp2 container state is not available, and says so* (memory_movers half)."""
    for window in PROMETHEUS_WINDOWS:
        result = await _tool().run(query_name="memory_movers", node="rp2", window=window)
        assert result.ok is True, "not derivable is a result, not a refusal"
        assert "rp2 runs no cadvisor" in result.content
        assert "no container metrics" in result.content
        assert "outside" not in result.content
        assert "Top" not in result.content and "moved by" not in result.content
    plan = plan_query("memory_movers", {"node": "rp2", "window": "1h"})
    assert plan.outcome is QueryOutcome.NOT_DERIVABLE
    assert plan.expressions == {}


@pytest.mark.parametrize("node", ["rp5", "rp2"])
async def test_host_service_state_is_not_available_where_the_collector_is_missing(node):
    for window in PROMETHEUS_WINDOWS:
        result = await _tool().run(query_name="host_service_state", node=node, window=window)
        assert result.ok is True
        assert "without the systemd collector" in result.content
        assert "outside" not in result.content
        assert "unit-state series" not in result.content
    plan = plan_query("host_service_state", {"node": node, "window": "24h"})
    assert plan.outcome is QueryOutcome.NOT_DERIVABLE
    assert plan.expressions == {}


async def test_host_service_state_on_the_vps_proceeds_against_its_node_exporter():
    recorder = Recorder()
    await _tool(recorder).run(query_name="host_service_state", node="vps", window="1h")
    queries = [r.url.params["query"] for r in recorder.requests]
    assert queries and all(f'job="{VPS_NODE_EXPORTER}"' in q for q in queries)


@pytest.mark.parametrize(
    "arguments",
    [
        {"query_name": "memory_movers", "node": "nas", "window": "1h"},
        {"query_name": "memory_movers", "node": "vps", "window": "7d"},
        {"query_name": "memory_movers", "node": "vps"},
        {"query_name": "host_service_state", "node": "nas", "window": "1h"},
        {"query_name": "host_service_state", "node": "vps", "window": "5m"},
        {"query_name": "host_service_state", "node": "vps", "window": "1h", "unit": "x"},
    ],
)
async def test_an_out_of_domain_host_coverage_value_is_refused_with_no_request(arguments):
    result = await _tool().run(**arguments)
    assert result.ok is False
    with pytest.raises(QueryRefused) as exc:
        plan_query(arguments["query_name"], {k: v for k, v in arguments.items() if k != "query_name"})
    assert exc.value.outcome is QueryOutcome.OUT_OF_DOMAIN


# =============================================================================
# 4.2 memory_movers
# =============================================================================


def _unit_id(unit: str) -> str:
    return f"/system.slice/{unit}"


def _container_id(name: str) -> str:
    return f"/system.slice/docker-{abs(hash(name)) % 10**12:012d}.scope"


def _instant_labels(*, unit: str | None = None, name: str | None = None) -> dict:
    """What `max_over_time`/`min_over_time` return: no `__name__`, no compose labels."""
    labels = {"job": VPS_CADVISOR, "instance": CADVISOR_INSTANCE}
    if unit:
        labels["id"] = _unit_id(unit)
    else:
        labels.update(id=_container_id(name), name=name, image="example.invalid/app:1")
    return labels


def _range_labels(*, unit: str | None = None, name: str | None = None) -> dict:
    """What the range query returns: `__name__` and the compose labels as well."""
    labels = {"__name__": "container_memory_working_set_bytes", **_instant_labels(unit=unit, name=name)}
    if name:
        labels["container_label_com_docker_compose_service"] = name
        labels["container_label_com_docker_compose_project"] = "example"
    return labels


class Cgroup:
    """One cgroup's working set on a window's grid, in MiB, with optional gaps."""

    def __init__(self, *, unit=None, name=None, mib: list[float], until: int | None = None):
        self.unit, self.name, self.mib, self.until = unit, name, mib, until

    def points(self, window: RangeWindow) -> list[tuple[float, float]]:
        times = grid(window)
        values = list(self.mib) + [self.mib[-1]] * (len(times) - len(self.mib))
        pairs = list(zip(times, values))
        return pairs[: self.until] if self.until is not None else pairs


def movers_payloads(window: RangeWindow, *cgroups: Cgroup) -> dict:
    maxima, minima, ranged = [], [], []
    for cgroup in cgroups:
        points = cgroup.points(window)
        values = [v * MIB for _t, v in points]
        labels = _instant_labels(unit=cgroup.unit, name=cgroup.name)
        maxima.append({"metric": labels, "value": [window.end, f"{max(values)}"]})
        minima.append({"metric": dict(labels), "value": [window.end, f"{min(values)}"]})
        ranged.append(
            {
                "metric": _range_labels(unit=cgroup.unit, name=cgroup.name),
                "values": [[t, f"{v * MIB}"] for t, v in points],
            }
        )
    return {
        "movers_max": vector(*maxima),
        "movers_min": vector(*minima),
        "movers_series": matrix(*ranged),
    }


def movers(window: str, *cgroups: Cgroup, max_points: int = 60, end: float = NOW) -> tuple[str, RangeWindow]:
    plan = planned("memory_movers", {"node": "vps", "window": window}, max_points=max_points, end=end)
    out = render_plan(plan, **movers_payloads(plan.range_window, *cgroups))
    return out, plan.range_window


def ranked_rows(out: str) -> list[str]:
    return [line for line in out.splitlines() if re.match(r"\s+\d\. ", line)]


def _burst(window: RangeWindow, *, low: float, high: float, peak_index: int) -> list[float]:
    """Flat at `low`, one peak at `high` on grid point `peak_index`, back to `low + 10`."""
    count = len(grid(window))
    values = [low] * count
    values[peak_index] = high
    for i in range(peak_index + 1, count):
        values[i] = low + 10
    return values


def test_a_host_units_page_cache_burst_is_named_with_its_peak_time():
    window = range_window("1h", NOW, 60)
    peak_index = 20
    out, window = movers(
        "1h",
        Cgroup(unit="example-a.service", mib=_burst(window, low=100, high=1000, peak_index=peak_index)),
        Cgroup(name="example-web", mib=[200.0, 230.0, 260.0]),
        Cgroup(name="example-db", mib=[400.0, 380.0]),
    )
    rows = ranked_rows(out)
    first = rows[0]
    assert first.lstrip().startswith("1. host unit `example-a.service`"), first
    assert "1000.0 MiB" in first, "the peak value"
    assert "100.0 MiB" in first, "the minimum"
    assert "moved by 900.0 MiB" in first
    assert f"peak at {stamp(grid(window)[peak_index])}" in first
    # Named as a host unit by its unit name, not by the cgroup path.
    assert "/system.slice" not in first
    assert "container `example-web`" in rows[1]


def test_range_functions_wrap_only_vector_selectors():
    entry = QUERY_REGISTRY["memory_movers"]
    selector = r'container_memory_working_set_bytes\{[^{}()\[\]]*\}'
    for role in ("movers_max", "movers_min"):
        template = entry.expressions[role]
        function = "max_over_time" if role == "movers_max" else "min_over_time"
        # Nothing but `f(<selector>[w]) or f(<selector>[w])`: the `or` is outside.
        assert re.fullmatch(
            rf"{function}\({selector}\[<window>\]\) or {function}\({selector}\[<window>\]\)",
            template,
        ), template
    for template in entry.expressions.values():
        # No range selector wraps a parenthesised expression (an HTTP 400).
        assert not re.search(r"\)\s*\[", template), template
        for match in re.finditer(r"_over_time\(", template):
            assert template[match.end()] != "(", template
    assert re.fullmatch(rf"{selector} or {selector}", entry.expressions["movers_series"])


def test_results_join_on_the_cgroup_id():
    window = range_window("6h", NOW, 60)
    out, window = movers(
        "6h",
        Cgroup(unit="example-a.service", mib=_burst(window, low=50, high=650, peak_index=10)),
        Cgroup(name="example-web", mib=_burst(window, low=300, high=500, peak_index=30)),
    )
    rows = ranked_rows(out)
    assert len(rows) == 2
    times = grid(window)
    for row, (low, high, index) in zip(rows, ((50, 650, 10), (300, 500, 30))):
        # Max, min and peak time, all three, on every ranked row.
        assert f"peak {high:.1f} MiB" in row, row
        assert f"min {low:.1f} MiB" in row, row
        assert f"peak at {stamp(times[index])}" in row, row
    assert "peak time not resolved" not in out


def test_a_row_whose_id_has_no_range_series_says_its_peak_time_is_unresolved():
    window = range_window("1h", NOW, 60)
    payloads = movers_payloads(window, Cgroup(unit="example-a.service", mib=[100.0, 500.0]))
    payloads["movers_series"] = matrix()
    plan = planned("memory_movers", {"node": "vps", "window": "1h"})
    out = render_plan(plan, **payloads)
    row = ranked_rows(out)[0]
    assert "peak 500.0 MiB" in row
    assert "peak time not resolved" in row


def test_a_cgroup_gone_at_the_windows_end_is_flagged():
    """Modelled on 2026-09-23: a host unit ranked first on 1h, no series at T."""
    window = range_window("1h", NOW, 60)
    count = len(grid(window))
    gone_at = count - 20  # its last sample is 20 points before the last range point
    out, window = movers(
        "1h",
        Cgroup(
            unit="example-a.service",
            mib=_burst(window, low=90, high=1100, peak_index=12),
            until=gone_at,
        ),
        Cgroup(name="example-web", mib=[200.0, 300.0]),
    )
    rows = ranked_rows(out)
    assert rows[0].lstrip().startswith("1. host unit `example-a.service`")
    assert "no series at the window's end (stopped or exited)" in rows[0]
    # It keeps its rank and its peak.
    assert "peak 1100.0 MiB" in rows[0]
    assert f"peak at {stamp(grid(window)[12])}" in rows[0]
    # A cgroup current to the last range point is not flagged.
    assert "no series at the window's end" not in rows[1]


def test_a_cgroup_current_to_the_last_range_point_is_not_flagged_on_an_uneven_step():
    """24h at 60 points: the last range point is 1430 s before `end`, not `end`."""
    window = range_window("24h", NOW, 60)
    assert window.end - window.last_point == 1430
    out, _ = movers("24h", Cgroup(unit="example-a.service", mib=[100.0, 900.0, 200.0]))
    assert "no series at the window's end" not in ranked_rows(out)[0]
    # One step short of the last point is gone.
    out, _ = movers(
        "24h",
        Cgroup(unit="example-a.service", mib=[100.0, 900.0, 200.0], until=len(grid(window)) - 1),
    )
    assert "no series at the window's end (stopped or exited)" in ranked_rows(out)[0]


#: The live clock is a float with sub-millisecond digits; Prometheus stamps
#: range points in milliseconds. A sample AT the last point then reads a
#: fraction of a millisecond early, which the half-step rule must absorb.
JITTERED_END = NOW + 0.1234567


def _millisecond_stamps(payload: dict) -> dict:
    for series in payload["data"]["result"]:
        if "values" in series:
            series["values"] = [[round(t, 3), v] for t, v in series["values"]]
    return payload


def test_a_cgroup_current_to_the_last_point_survives_millisecond_stamps():
    plan = planned("memory_movers", {"node": "vps", "window": "1h"}, end=JITTERED_END)
    payloads = movers_payloads(plan.range_window, Cgroup(unit="example-a.service", mib=[100.0, 800.0]))
    payloads["movers_series"] = _millisecond_stamps(payloads["movers_series"])
    last = payloads["movers_series"]["data"]["result"][0]["values"][-1][0]
    assert last < plan.range_window.last_point, "the fixture must read early"
    out = render_plan(plan, **payloads)
    assert "no series at the window's end" not in ranked_rows(out)[0]


def test_containers_and_host_units_rank_together():
    movements = [
        Cgroup(unit="example-a.service", mib=[100.0, 700.0]),  # 600
        Cgroup(name="example-web", mib=[100.0, 600.0]),  # 500
        Cgroup(unit="example-b.service", mib=[50.0, 450.0]),  # 400
        Cgroup(name="quirky_hopper", mib=[10.0, 310.0]),  # 300
        Cgroup(unit="example-c.service", mib=[20.0, 220.0]),  # 200
        Cgroup(name="example-db", mib=[500.0, 600.0]),  # 100, sixth: not shown
    ]
    out, _ = movers("1h", *movements)
    rows = ranked_rows(out)
    assert len(rows) == 5, "the top five, never the whole population"
    expected = [
        "host unit `example-a.service`",
        "container `example-web`",
        "host unit `example-b.service`",
        "container `quirky_hopper`",
        "host unit `example-c.service`",
    ]
    for index, (row, label) in enumerate(zip(rows, expected), start=1):
        assert row.lstrip().startswith(f"{index}. {label}"), row
    assert "example-db" not in out
    # D3's hedged auto-name annotation applies to the container rows.
    assert "looks auto-generated" in rows[3]
    assert "looks auto-generated" not in rows[1]
    assert "6 cgroups" in out and "3 host units" in out and "3 containers" in out


def test_peak_time_resolution_is_stated():
    out, window = movers("6h", Cgroup(unit="example-a.service", mib=[100.0, 400.0]))
    step = window.step
    assert step == 367, "6h at 60 points: ceil(21600 / 59)"
    assert f"accurate to within one range step ({step} s)" in out
    # The same statement follows the budget, not a literal.
    out, window = movers("6h", Cgroup(unit="example-a.service", mib=[100.0, 400.0]), max_points=30)
    assert window.step == 745
    assert "accurate to within one range step (745 s)" in out


def test_a_plan_without_its_window_states_no_resolution_and_no_end():
    plan = plan_query("memory_movers", {"node": "vps", "window": "1h"})
    window = range_window("1h", NOW, 60)
    out = render_plan(plan, **movers_payloads(window, Cgroup(unit="example-a.service", mib=[1.0, 9.0])))
    assert "accurate to within one range step (" not in out
    assert "no series at the window's end (stopped or exited)" not in out
    assert "window was not supplied" in out


def test_no_series_is_not_an_empty_ranking():
    plan = planned("memory_movers", {"node": "vps", "window": "1h"})
    out = render_plan(plan, movers_max=vector(), movers_min=vector(), movers_series=matrix())
    assert "could not be derived" in out
    assert "no cgroup series were returned" in out
    assert not ranked_rows(out)
    assert "Top" not in out


def test_an_unreadable_movers_response_is_not_an_empty_ranking():
    plan = planned("memory_movers", {"node": "vps", "window": "1h"})
    out = render_plan(plan, movers_max={"status": "error"}, movers_min=None, movers_series=None)
    assert "could not be derived" in out
    assert not ranked_rows(out)


def test_an_unreadable_range_answer_resolves_no_peak_time_and_flags_nothing():
    window = range_window("1h", NOW, 60)
    payloads = movers_payloads(window, Cgroup(unit="example-a.service", mib=[100.0, 500.0]))
    payloads["movers_series"] = {"status": "error", "error": "x"}
    out = render_plan(planned("memory_movers", {"node": "vps", "window": "1h"}), **payloads)
    row = ranked_rows(out)[0]
    assert "peak 500.0 MiB" in row
    assert "the range answer could not be read" in row
    # No range answer is not evidence that the cgroup is gone.
    assert "no series at the window's end" not in row


def test_the_peak_value_is_the_exact_max_not_the_highest_range_point():
    """A burst between two grid points: max_over_time sees it, the range does not."""
    window = range_window("1h", NOW, 60)
    payloads = movers_payloads(window, Cgroup(unit="example-a.service", mib=[100.0, 400.0, 150.0]))
    exact = 950.0 * MIB
    payloads["movers_max"]["data"]["result"][0]["value"][1] = f"{exact}"
    out = render_plan(planned("memory_movers", {"node": "vps", "window": "1h"}), **payloads)
    row = ranked_rows(out)[0]
    assert "peak 950.0 MiB" in row and "moved by 850.0 MiB" in row
    # The time is still the highest range point's, one step coarse.
    assert f"peak at {stamp(grid(window)[1])}" in row


def test_memory_movers_returns_no_raw_series_and_carries_its_caveats():
    window = range_window("1h", NOW, 60)
    marked = [100.0 + i * 0.25 for i in range(len(grid(window)))]
    out, _ = movers("1h", Cgroup(unit="example-a.service", mib=marked))
    # Intermediate samples never reach the result.
    shown = {f"{marked[0]:.1f}", f"{marked[-1]:.1f}"}
    for value in marked[1:-1]:
        if f"{value:.1f}" not in shown:
            assert f" {value:.1f} MiB" not in out, value
    assert "Working set includes active page cache" in out
    assert "moved from absent" in out
    assert "A stopped container has no series" in out
    assert "no bar" in out.lower()
    assert "Compared against" not in out
    assert f"Window ends at {stamp(NOW)}" in out


async def test_the_point_budget_holds_at_24h():
    for max_points in (60, 30, 10):
        recorder = Recorder()
        await _tool(recorder, max_points=max_points).run(
            query_name="memory_movers", node="vps", window="24h"
        )
        ranged = [r for r in recorder.requests if r.url.path.endswith("/query_range")]
        instant = [r for r in recorder.requests if r.url.path.endswith("/query")]
        assert len(ranged) == 1 and len(instant) == 2
        params = ranged[0].url.params
        span = float(params["end"]) - float(params["start"])
        assert span == 86400
        assert span // float(params["step"]) + 1 <= max_points
        assert float(params["end"]) == NOW
        for request in instant:
            assert set(request.url.params) == {"query"}


async def test_memory_movers_through_the_tool_renders_a_ranking():
    window = range_window("1h", NOW, 60)
    payloads = movers_payloads(window, Cgroup(unit="example-a.service", mib=[100.0, 900.0]))

    def answer(request):
        query = request.url.params["query"]
        if query.startswith("max_over_time"):
            return payloads["movers_max"]
        if query.startswith("min_over_time"):
            return payloads["movers_min"]
        return payloads["movers_series"]

    result = await _tool(Recorder(answer)).run(query_name="memory_movers", node="vps", window="1h")
    assert result.ok, result.error
    assert "1. host unit `example-a.service`" in result.content
    assert_no_addresses(result.content)


# =============================================================================
# 4.3 host_service_state
# =============================================================================


def _unit_series(unit: str, state: str, indices: list[int], window: RangeWindow) -> dict:
    times = grid(window)
    return {
        "metric": {
            "__name__": "node_systemd_unit_state",
            "job": VPS_NODE_EXPORTER,
            "instance": NODE_INSTANCE,
            "name": unit,
            "state": state,
            "type": "simple",
        },
        "values": [[times[i], "1"] for i in indices],
    }


def _count(value: float | None) -> dict:
    if value is None:
        return vector()
    return vector({"metric": {}, "value": [NOW, f"{value:g}"]})


def services(window: str, *series_for, unit_count: float | None = 1320, max_points: int = 60, end: float = NOW):
    plan = planned("host_service_state", {"node": "vps", "window": window}, max_points=max_points, end=end)
    bad = matrix(*(make(plan.range_window) for make in series_for))
    return render_plan(plan, bad_states=bad, unit_count=_count(unit_count)), plan.range_window


@pytest.mark.parametrize("max_points", sorted(EXPECTED_24H_POINTS))
def test_a_persistently_failed_unit_is_reported_with_its_fraction(max_points):
    expected = EXPECTED_24H_POINTS[max_points]
    out, window = services(
        "24h",
        lambda w: _unit_series("example-a.service", "failed", list(range(len(grid(w)))), w),
        max_points=max_points,
    )
    assert len(grid(window)) == expected
    line = next(l for l in out.splitlines() if "example-a.service" in l)
    assert "state failed" in line
    assert f"failed in {expected}/{expected} samples over 24h" in line
    assert "still failed at the window's end" in line
    assert "probable crash loop" not in line


def test_a_crash_looping_unit_is_flagged():
    """281/288 at a 5-minute step; at 60 points the same share is 57/59."""
    out, window = services(
        "24h",
        lambda w: _unit_series("example-b.service", "activating", [i for i in range(59) if i not in (3, 30)], w),
    )
    line = next(l for l in out.splitlines() if "example-b.service" in l)
    assert "probable crash loop" in line
    assert "activating in 57/59 samples over 24h" in line


def test_a_minority_of_activating_samples_is_not_called_a_crash_loop():
    out, _ = services(
        "24h", lambda w: _unit_series("example-c.service", "activating", [10, 11, 12], w)
    )
    line = next(l for l in out.splitlines() if "example-c.service" in l)
    assert "activating in 3/59 samples over 24h" in line
    assert "probable crash loop" not in line
    assert "not activating at the window's end" in line


def test_exactly_half_activating_is_not_most_of_the_window():
    # 1h at a 10-point budget: step 400 s, 10 points, so half is a whole count.
    # (Every window at 60 points has an odd count, so it cannot test the edge.)
    out, window = services(
        "1h",
        lambda w: _unit_series("example-c.service", "activating", list(range(5, 10)), w),
        max_points=10,
    )
    assert len(grid(window)) == 10
    line = next(l for l in out.splitlines() if "example-c.service" in l)
    assert "5/10" in line and "probable crash loop" not in line
    out, _ = services(
        "1h",
        lambda w: _unit_series("example-c.service", "activating", list(range(4, 10)), w),
        max_points=10,
    )
    line = next(l for l in out.splitlines() if "example-c.service" in l)
    assert "6/10" in line and "probable crash loop" in line


def test_a_unit_that_recovered_is_not_still_failed_at_the_end():
    out, _ = services(
        "24h", lambda w: _unit_series("example-a.service", "failed", list(range(0, 40)), w)
    )
    line = next(l for l in out.splitlines() if "example-a.service" in l)
    assert "failed in 40/59 samples over 24h" in line
    assert "not failed at the window's end" in line
    assert "still failed" not in line


def test_a_healthy_host_proves_the_query_ran():
    out, _ = services("24h", unit_count=1320)
    assert "could not be derived" not in out
    assert "No unit was failed or activating" in out
    assert "1320 unit-state series" in out


def test_the_unit_count_is_series_never_units():
    """D5's `count(...)` is units x states (264 x 5 = 1320 on the vps, 1.1)."""
    for out, _ in (
        services("24h", unit_count=1320),
        services("1h", lambda w: _unit_series("example-a.service", "failed", [0, 1, 2], w), unit_count=1320),
    ):
        assert "1320 unit-state series" in out
        assert not re.search(r"\b1320 units\b", out)
        assert not re.search(r"\b264 units\b", out), "no unit count derived from a state count"
        assert not re.search(r"\b\d+ (?:systemd )?units\b", out)


@pytest.mark.parametrize("unit_count", [0, None])
def test_a_silent_collector_is_not_health(unit_count):
    out, _ = services("24h", unit_count=unit_count)
    assert "could not be derived" in out
    assert "the systemd collector reported no units" in out
    assert "No unit was failed or activating" not in out


def test_a_silent_collector_is_not_derivable_even_beside_bad_states():
    out, _ = services(
        "1h", lambda w: _unit_series("example-a.service", "failed", [0], w), unit_count=None
    )
    assert "could not be derived" in out
    assert "the systemd collector reported no units" in out


def test_an_unreadable_bad_state_response_is_not_health():
    plan = planned("host_service_state", {"node": "vps", "window": "1h"})
    out = render_plan(plan, bad_states={"status": "error"}, unit_count=_count(1320))
    assert "could not be derived" in out
    assert "No unit was failed or activating" not in out


def test_only_samples_in_the_state_are_counted():
    """`== 1` filters server-side; a 0 point that reached the renderer is not counted."""
    def with_zeros(w):
        series = _unit_series("example-a.service", "failed", list(range(10)), w)
        times = grid(w)
        series["values"] += [[times[i], "0"] for i in range(10, 59)]
        return series

    out, _ = services("24h", with_zeros)
    line = next(l for l in out.splitlines() if "example-a.service" in l)
    assert "failed in 10/59 samples over 24h" in line
    assert "not failed at the window's end" in line


def test_a_unit_current_to_the_last_point_survives_millisecond_stamps():
    plan = planned("host_service_state", {"node": "vps", "window": "24h"}, end=JITTERED_END)
    bad = _millisecond_stamps(
        matrix(_unit_series("example-a.service", "failed", list(range(59)), plan.range_window))
    )
    assert bad["data"]["result"][0]["values"][-1][0] < plan.range_window.last_point
    out = render_plan(plan, bad_states=bad, unit_count=_count(1320))
    line = next(l for l in out.splitlines() if "example-a.service" in l)
    assert "still failed at the window's end" in line


def test_unit_names_are_scrubbed():
    out, _ = services(
        "1h",
        lambda w: _unit_series("example-vpn@192.0.2.9.service", "failed", list(range(len(grid(w)))), w),
    )
    assert "192.0.2.9" not in out
    assert "example-vpn@" in out


def test_host_service_state_states_its_window_and_caveats():
    out, _ = services("6h", lambda w: _unit_series("example-a.service", "failed", [0], w))
    assert f"Window ends at {stamp(NOW)}" in out
    assert "measurement" in out and "no alert rule" in out.lower()
    assert "once per state" in out


def test_a_host_service_plan_without_its_window_states_no_fraction():
    plan = plan_query("host_service_state", {"node": "vps", "window": "1h"})
    window = range_window("1h", NOW, 60)
    bad = matrix(_unit_series("example-a.service", "failed", [0, 1], window))
    out = render_plan(plan, bad_states=bad, unit_count=_count(1320))
    line = next(l for l in out.splitlines() if "example-a.service" in l)
    assert "failed in 2 samples" in line
    assert "/" not in line.split("samples")[0].split("failed in")[-1]
    assert "window was not supplied" in out


# =============================================================================
# 4.4 The D5 contract, the capture's retirement, derived descriptions
# =============================================================================


def test_the_host_coverage_templates_are_byte_equal_to_the_d5_literals():
    for (query, role), (kind, literal) in D5_HOST_COVERAGE_TEMPLATES.items():
        entry = QUERY_REGISTRY[query]
        assert entry.expressions[role] == literal, (query, role)
        assert entry.range_query is True
        assert (role in entry.range_roles) is (kind == "range"), (query, role)
    # Each entry holds exactly the D5 roles, nothing more.
    for query in ("memory_movers", "host_service_state"):
        roles = {r for q, r in D5_HOST_COVERAGE_TEMPLATES if q == query}
        assert set(QUERY_REGISTRY[query].expressions) == roles
        assert QUERY_REGISTRY[query].routes == {}
    # The one spelling of the host-unit regex, and the unescaped `|`.
    host_unit = r'"/system\\.slice/.+\\.service"'
    for role in ("movers_max", "movers_min", "movers_series"):
        template = QUERY_REGISTRY["memory_movers"].expressions[role]
        assert template.count(host_unit) == 1
        assert "\\\\\\\\" not in template
    assert 'state=~"activating|failed"' in QUERY_REGISTRY["host_service_state"].expressions["bad_states"]
    assert "\\|" not in QUERY_REGISTRY["host_service_state"].expressions["bad_states"]


def test_the_capture_reads_every_template_from_the_registry():
    assert not hasattr(capture, "WRITTEN_OUT_TEMPLATES"), "the written-out copies are retired"
    assert not hasattr(capture, "WRITTEN_OUT_DOMAINS")
    requests = capture.plan_requests()
    assert {r.source for r in requests} == {"registry"}
    sent = {(r.query, r.role, r.arguments): (r.kind, r.expression) for r in requests}
    job_maps = {"memory_movers": CADVISOR_JOBS, "host_service_state": NODE_EXPORTER_JOBS}
    nodes = {"memory_movers": ("rp5", "vps"), "host_service_state": ("vps",)}
    expected = {}
    for (query, role), (kind, literal) in D5_HOST_COVERAGE_TEMPLATES.items():
        for node in nodes[query]:
            for window in PROMETHEUS_WINDOWS:
                filled = literal.replace("<job>", job_maps[query][node]).replace("<window>", window)
                expected[(query, role, (("node", node), ("window", window)))] = (kind, filled)
    got = {k: v for k, v in sent.items() if k[0] in job_maps}
    assert got == expected


def test_the_capture_count_per_T_is_unchanged_by_the_retirement():
    # 155 static requests per T, as the rp5 capture sent (evidence-probe 1b.4).
    assert len(capture.plan_requests()) == 155


def test_every_host_coverage_template_stays_address_free():
    for query in ("memory_movers", "host_service_state"):
        for template in QUERY_REGISTRY[query].templates.values():
            assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b|://", template)
            assert "instance" not in template


def test_the_renderers_are_named_after_their_entries():
    for query in ("memory_movers", "host_service_state"):
        assert QUERY_REGISTRY[query].renderer.__name__ == f"render_{query}"


_COUNT_WORDS = (
    "one two three four five six seven eight nine ten eleven twelve".split()
)


def test_the_prompt_summary_carries_no_hand_maintained_query_count():
    """The enum is advertised by the schema, derived from the registry.

    A count typed into the prompt ("one of six named queries") went stale the
    moment two queries joined; the summary names none, so it cannot drift.
    """
    (_name, summary), = QUERY_TOOL_SUMMARIES
    for word in _COUNT_WORDS:
        assert not re.search(rf"\b{word} named\b", summary), summary
    assert not re.search(r"\b\d+ named\b", summary)
    prompt = build_system_prompt(homelab_query_enabled=True)
    assert "one of six named" not in prompt


def test_the_tool_description_and_refusal_derive_the_enum_from_the_registry():
    from henk.tools.homelab_query import HomelabQueryTool as Tool

    for word in _COUNT_WORDS:
        assert not re.search(rf"\b{word} (?:named|queries)\b", Tool.description)
    description = Tool.parameters["properties"]["query_name"]["description"]
    for name in QUERY_NAMES:
        assert f"{name}: " in description
    with pytest.raises(QueryRefused) as exc:
        plan_query("alerts_firing", {})
    message = str(exc.value)
    for name in QUERY_NAMES:
        assert name in message
    assert "six" not in message
    assert f"{len(QUERY_NAMES)} named queries" in message
