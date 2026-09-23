"""The first-case capture script (triage-quality task 1b.1).

From `design.md` D5 and D15: "Instant captures are pinned to the evaluation
time", "The capture covers every argument combination", "The capture refuses an
unsafe output path".

**Every request assertion reads the transport.** The capture is handed a client
whose transport records what it was asked for, so "the instant query carried
`time=T`" is a property of the HTTP traffic, not of a data structure the script
could build correctly and then fail to send.

**The written-out templates are literals in this file.** They are never parsed
from `design.md`, which moves at archive. They are the D5 canonical table, byte
for byte, and the host-unit regex has exactly one spelling:
`/system\\.slice/.+\\.service`, a backslash-escaped dot inside a PromQL
double-quoted string.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from henk.replay import capture
from henk.tools import query_registry
from henk.tools.homelab_query import HomelabQueryTool
from henk.tools.query_registry import (
    CADVISOR_JOBS,
    NODE_EXPORTER_JOBS,
    PROMETHEUS_WINDOWS,
    QUERY_REGISTRY,
    QueryBackend,
    QueryEntry,
    QueryOutcome,
    QueryParameter,
    Unavailable,
    WINDOW_SECONDS,
    named_container_expression,
    plan_query,
    range_step_seconds,
)

# RFC 5737 placeholder, per this change's standing rule 1.
PROMETHEUS = "http://192.0.2.10:9090"

#: The three evaluation times of the 2026-09-23 case (notes/evidence-probe.md,
#: 1b.3). Times only; they carry nothing about the incident.
T_START = 1790144998
T_MID = 1790145012
T_END = 1790145026

# --- The D5 canonical table, hardcoded ------------------------------------

UNITS = r'container_memory_working_set_bytes{job="<job>",id=~"/system\\.slice/.+\\.service"}'
NAMED = 'container_memory_working_set_bytes{job="<job>",name!=""}'

D5_TEMPLATES = {
    ("container_state", "memory_working_set"): (
        "instant",
        'container_memory_working_set_bytes{job="<job>",name!=""}',
    ),
    ("container_state", "swap"): (
        "instant",
        'container_memory_swap{job="<job>",name!=""}',
    ),
    ("container_state", "restarts_15m"): (
        "instant",
        'max by (name) (resets(container_cpu_usage_seconds_total{job="<job>",name!=""}[15m]))',
    ),
    ("container_state", "restarts_24h"): (
        "instant",
        'max by (name) (resets(container_cpu_usage_seconds_total{job="<job>",name!=""}[24h]))',
    ),
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

#: Container names the fake cadvisor reports, per node. Invented placeholders.
NAMES_BY_JOB = {
    "cadvisor-pi5": ("example-a", "example-b"),
    "cadvisor-vps": ("example-c",),
}

#: The per-`T` request count, derived by hand from the registry and D5 so the
#: test does not recompute it with the code under test:
#:   node_resource_trend  3 nodes x 7 resources x 4 windows, minus vps
#:                        temperature (not derivable) at 4 windows     = 80
#:   scrape_targets       up, up_over_window                            =  2
#:   freshness_check      timestamps                                    =  1
#:   container_state      2 nodes (rp2 not derivable) x 8 roles         = 16
#:   dns_performance      3 nodes x 4 windows x 2 roles                 = 24
#:   memory_movers        2 nodes (rp2 unavailable) x 4 windows x 3     = 24
#:   host_service_state   vps only x 4 windows x 2 roles                =  8
#:                                                                       ---
#:                                                                       155
#: plus one named-container follow-up per name in that `T`'s results.
STATIC_REQUESTS_PER_T = 155
PER_QUERY = {
    "node_resource_trend": 80,
    "scrape_targets": 2,
    "freshness_check": 1,
    "container_state": 16,
    "dns_performance": 24,
    "memory_movers": 24,
    "host_service_state": 8,
}


class FakePrometheus:
    """Records every request; answers cadvisor selectors with container names."""

    def __init__(self, *, names_by_job=None, status_for=None):
        self.requests: list[httpx.Request] = []
        self._names = NAMES_BY_JOB if names_by_job is None else names_by_job
        self._status_for = status_for or (lambda query: 200)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = _params(request)
        query = params.get("query", "")
        status = self._status_for(query)
        if status != 200:
            return httpx.Response(
                status, json={"status": "error", "errorType": "bad_data", "error": "x"}
            )
        result = []
        names = self._names(params) if callable(self._names) else self._names
        for job, container_names in names.items():
            if f'job="{job}"' in query and 'name!=""' in query:
                result = [
                    {"metric": {"name": n, "job": job}, "value": [0, "1"]}
                    for n in container_names
                ]
        kind = "matrix" if request.url.path.endswith("query_range") else "vector"
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": kind, "result": result}}
        )


def _params(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    path = tmp_path / "scratch"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _run(out_dir: Path, handler=None, *, at=(T_START,), max_points=60):
    handler = handler or FakePrometheus()
    manifests = capture.run_capture(
        _client(handler),
        prometheus_url=PROMETHEUS,
        evaluation_times=at,
        max_points=max_points,
        out_dir=out_dir,
    )
    return handler, manifests


def _records(out_dir: Path, t: int) -> list[dict]:
    directory = out_dir / capture.time_label(t)
    return [
        json.loads(path.read_text())
        for path in sorted(directory.glob("*.json"))
        if path.name != capture.MANIFEST_NAME
    ]


# --- Instant captures are pinned to the evaluation time -------------------


def test_instant_queries_carry_time_T_and_nothing_else(out_dir):
    fake, _ = _run(out_dir)
    instant = [r for r in fake.requests if r.url.path == "/api/v1/query"]
    assert instant, "the capture issued no instant query at all"
    for request in instant:
        params = _params(request)
        assert set(params) == {"query", "time"}, params
        assert params["time"] == str(T_START)


@pytest.mark.parametrize("max_points", [60, 30])
def test_range_queries_end_at_T_with_the_registry_step(out_dir, max_points):
    fake, _ = _run(out_dir, max_points=max_points)
    ranged = [r for r in fake.requests if r.url.path == "/api/v1/query_range"]
    assert ranged, "the capture issued no range query at all"
    windows_seen = set()
    for request in ranged:
        params = _params(request)
        assert set(params) == {"query", "start", "end", "step"}, params
        assert int(params["end"]) == T_START
        span = T_START - int(params["start"])
        window = next(w for w, s in WINDOW_SECONDS.items() if s == span)
        windows_seen.add(window)
        assert int(params["step"]) == range_step_seconds(window, max_points)
    assert windows_seen == set(PROMETHEUS_WINDOWS)


def test_every_registry_request_matches_what_the_live_tool_would_send(out_dir):
    """The capture's routing, step and span are the tool's own.

    `HomelabQueryTool._prometheus_request` is the live seam; with its clock
    pinned to `T` it must build the request the capture sent, for every plan
    the registry produces. An instant plan must stay instant (the tool sends no
    `time=`, the capture adds exactly that), so `dns_performance`'s
    `node_mapping` cannot quietly become a range query here.
    """
    fake, _ = _run(out_dir)
    sent = {
        (r.url.path, tuple(sorted(_params(r).items()))) for r in fake.requests
    }
    tool = HomelabQueryTool(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None)),
        gatus_url="http://192.0.2.11:8080",
        prometheus_url=PROMETHEUS,
        max_points=60,
        clock=lambda: T_START,
    )
    checked = {"range": 0, "instant": 0}
    for name, entry in QUERY_REGISTRY.items():
        if entry.backend is not QueryBackend.PROMETHEUS:
            continue
        for arguments in _combinations(entry):
            plan = plan_query(name, arguments)
            if plan.outcome is not QueryOutcome.ANSWERED:
                continue
            for role, expression in plan.expressions.items():
                url, params = tool._prometheus_request(plan, expression, role)
                if url.endswith("/query_range"):
                    kind, want = "range", dict(params)
                else:
                    assert set(params) == {"query"}
                    kind, want = "instant", {**params, "time": T_START}
                key = (
                    url.removeprefix(PROMETHEUS),
                    tuple(sorted((k, v if k == "query" else str(int(v))) for k, v in want.items())),
                )
                assert key in sent, (name, arguments, role)
                checked[kind] += 1
    # 80 trends + 12 dns series are ranges; 2 + 1 + 16 + 12 are instant. The
    # container count is 16 since task 3.9 moved D3/D4's four roles into the
    # registry (it was 8 while the script wrote them out).
    assert checked == {"range": 92, "instant": 31}


def test_only_the_two_prometheus_query_routes_are_requested(out_dir):
    fake, _ = _run(out_dir)
    paths = {r.url.path for r in fake.requests}
    assert paths == {"/api/v1/query", "/api/v1/query_range"}
    assert all(str(r.url).startswith(PROMETHEUS) for r in fake.requests)


# --- The capture covers every argument combination ------------------------


def _combinations(entry: QueryEntry):
    names = [p.name for p in entry.parameters]
    domains = [p.domain for p in entry.parameters]
    for values in itertools.product(*domains):
        yield dict(zip(names, values))


def test_the_request_count_per_T_is_pinned(out_dir):
    _, manifests = _run(out_dir, at=(T_START, T_END))
    names = sum(len(v) for v in NAMES_BY_JOB.values())
    for manifest in manifests:
        assert manifest["request_count"] == STATIC_REQUESTS_PER_T + names
        per_query = dict(manifest["per_query"])
        assert per_query.pop("container_state_named") == names
        assert per_query == PER_QUERY


def test_every_registry_prometheus_expression_is_captured_in_every_combination(out_dir):
    _run(out_dir)
    captured = {
        (r["query"], r["role"], tuple(sorted(r["arguments"].items())), r["expression"])
        for r in _records(out_dir, T_START)
    }
    expected = 0
    for name, entry in QUERY_REGISTRY.items():
        if entry.backend is not QueryBackend.PROMETHEUS:
            continue
        for arguments in _combinations(entry):
            plan = plan_query(name, arguments)
            if plan.outcome is not QueryOutcome.ANSWERED:
                continue
            for role, expression in plan.expressions.items():
                key = (name, role, tuple(sorted(plan.arguments.items())), expression)
                assert key in captured, key
                expected += 1
    # scrape_targets' two expressions are named explicitly: they are the part
    # of that query that has a historical form.
    roles = {(q, r) for q, r, _, _ in captured}
    assert {("scrape_targets", "up"), ("scrape_targets", "up_over_window")} <= roles
    assert expected == 80 + 2 + 1 + 16 + 24


def test_gatus_and_the_targets_api_are_never_captured(out_dir):
    _run(out_dir)
    queries = {r["query"] for r in _records(out_dir, T_START)}
    assert "endpoint_history" not in queries
    assert all("targets" not in r["request"]["path"] for r in _records(out_dir, T_START))


#: The D5 rows task 3.9 moved into the registry and retired from the script.
RETIRED_TO_REGISTRY = frozenset(
    {
        ("container_state", "memory_working_set"),
        ("container_state", "swap"),
        ("container_state", "restarts_15m"),
        ("container_state", "restarts_24h"),
    }
)


def test_the_written_out_templates_are_byte_equal_to_the_D5_table():
    """Every D5 row lives in exactly one place, byte-equal to the literal.

    Task 3.9 retired the four `container_state` rows: they are pinned in the
    REGISTRY now, and the script writes out only the rows the registry lacks.
    """
    table = {
        (t.query, t.role): (t.kind, t.template) for t in capture.WRITTEN_OUT_TEMPLATES
    }
    assert table == {k: v for k, v in D5_TEMPLATES.items() if k not in RETIRED_TO_REGISTRY}
    for query, role in RETIRED_TO_REGISTRY:
        kind, literal = D5_TEMPLATES[(query, role)]
        entry = QUERY_REGISTRY[query]
        assert entry.expressions[role] == literal, (query, role)
        assert kind == "instant" and not entry.range_query
    # Retired means retired: no D5 row is in both places.
    for query, role in table:
        entry = QUERY_REGISTRY.get(query)
        assert entry is None or role not in entry.expressions, (query, role)
    host_unit = r'"/system\\.slice/.+\\.service"'
    for kind, template in D5_TEMPLATES.values():
        if "id=~" in template:
            assert host_unit in template
            # Two literal backslashes on the wire, never one and never four.
            assert template.count("\\\\.") == 2 * template.count("id=~")
            assert "\\\\\\\\" not in template


def test_the_written_out_domains_follow_the_design():
    domains = capture.WRITTEN_OUT_DOMAINS
    movers = domains["memory_movers"]
    assert movers.nodes == ("rp5", "vps", "rp2")
    assert dict(movers.job_map) == dict(CADVISOR_JOBS)
    assert set(movers.unavailable) == {"rp2"}
    assert movers.windows == PROMETHEUS_WINDOWS
    services = domains["host_service_state"]
    assert services.nodes == ("rp5", "vps", "rp2")
    assert dict(services.job_map) == dict(NODE_EXPORTER_JOBS)
    assert set(services.unavailable) == {"rp5", "rp2"}
    assert services.windows == PROMETHEUS_WINDOWS


def test_movers_apply_the_range_function_per_selector(out_dir):
    """`max_over_time((a or b)[w])` is an HTTP 400; the capture never sends it."""
    _run(out_dir)
    records = _records(out_dir, T_START)
    for record in records:
        assert not re.search(r"\)\s*\[", record["expression"]), record["expression"]
    movers = [r for r in records if r["query"] == "memory_movers"]
    assert {r["role"] for r in movers} == {"movers_max", "movers_min", "movers_series"}
    for record in movers:
        if record["role"] == "movers_max":
            window = record["arguments"]["window"]
            assert record["expression"].count(f"[{window}])") == 2
            assert record["expression"].count("max_over_time(") == 2


def test_no_request_for_the_unavailable_nodes(out_dir):
    _run(out_dir)
    records = _records(out_dir, T_START)
    assert not [
        r for r in records
        if r["query"] == "memory_movers" and r["arguments"]["node"] == "rp2"
    ]
    assert {
        r["arguments"]["node"] for r in records if r["query"] == "host_service_state"
    } == {"vps"}
    assert not [
        r for r in records
        if r["query"] == "node_resource_trend"
        and r["arguments"]["node"] == "vps"
        and r["arguments"]["resource"] == "temperature"
    ]


def test_the_written_out_expressions_are_filled_per_node_and_window(out_dir):
    _run(out_dir)
    records = _records(out_dir, T_START)
    got = {
        (r["query"], r["role"], tuple(sorted(r["arguments"].items()))): r["expression"]
        for r in records
        if (r["query"], r["role"]) in D5_TEMPLATES
    }
    job_maps = {
        "container_state": CADVISOR_JOBS,
        "memory_movers": CADVISOR_JOBS,
        "host_service_state": NODE_EXPORTER_JOBS,
    }
    for (query, role, arguments), expression in got.items():
        args = dict(arguments)
        want = D5_TEMPLATES[(query, role)][1].replace("<job>", job_maps[query][args["node"]])
        if "window" in args:
            want = want.replace("<window>", args["window"])
        assert expression == want
    assert len(got) == 8 + 24 + 8
    for record in records:
        if (record["query"], record["role"]) in D5_TEMPLATES:
            retired = (record["query"], record["role"]) in RETIRED_TO_REGISTRY
            assert record["source"] == ("registry" if retired else "written-out")
            want_kind = D5_TEMPLATES[(record["query"], record["role"])][0]
            assert record["kind"] == want_kind
            assert record["request"]["path"] == (
                "/api/v1/query_range" if want_kind == "range" else "/api/v1/query"
            )


# --- Retirement: the count does not double once groups 3 and 4 land -------


def _registry_with_the_d5_roles() -> dict[str, QueryEntry]:
    """The registry as groups 3 and 4 will leave it, for these roles only."""
    def fill_roles(query: str) -> dict[str, str]:
        return {role: tpl for (q, role), (_, tpl) in D5_TEMPLATES.items() if q == query}

    container = QUERY_REGISTRY["container_state"]
    node_param = QueryParameter("node", ("rp5", "vps", "rp2"), "Which node.")
    window_param = QueryParameter("window", PROMETHEUS_WINDOWS, "How far back.")
    return {
        # D3's domain change comes with it: rp2 in domain, not derivable.
        "container_state": dataclasses.replace(
            container,
            expressions={**container.expressions, **fill_roles("container_state")},
            parameters=(node_param,),
            unavailable=container.unavailable
            + (Unavailable(parameters={"node": "rp2"}, aspect=None, reason="no cadvisor."),),
        ),
        "memory_movers": QueryEntry(
            name="memory_movers",
            backend=QueryBackend.PROMETHEUS,
            summary="movers",
            expressions=fill_roles("memory_movers"),
            parameters=(node_param, window_param),
            job_map=CADVISOR_JOBS,
            range_query=True,
            range_roles=frozenset({"movers_series"}),
            unavailable=(Unavailable(parameters={"node": "rp2"}, aspect=None, reason="x."),),
        ),
        "host_service_state": QueryEntry(
            name="host_service_state",
            backend=QueryBackend.PROMETHEUS,
            summary="services",
            expressions=fill_roles("host_service_state"),
            parameters=(node_param, window_param),
            job_map=NODE_EXPORTER_JOBS,
            range_query=True,
            range_roles=frozenset({"bad_states"}),
            unavailable=(
                Unavailable(parameters={"node": "rp5"}, aspect=None, reason="x."),
                Unavailable(parameters={"node": "rp2"}, aspect=None, reason="x."),
            ),
        ),
    }


def _request_set(records):
    return {
        (
            r["query"],
            r["role"],
            tuple(sorted(r["arguments"].items())),
            r["expression"],
            r["request"]["path"],
            tuple(sorted(r["request"]["params"].items())),
        )
        for r in records
    }


def test_the_count_is_unchanged_once_the_registry_has_the_roles(tmp_path, monkeypatch):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    for d in (before_dir, after_dir):
        d.mkdir(mode=0o700)
        d.chmod(0o700)
    _, before = _run(before_dir)
    for name, entry in _registry_with_the_d5_roles().items():
        monkeypatch.setitem(query_registry._REGISTRY, name, entry)
    _, after = _run(after_dir)

    assert after[0]["request_count"] == before[0]["request_count"]
    assert after[0]["per_query"] == before[0]["per_query"]
    after_records = _records(after_dir, T_START)
    assert _request_set(after_records) == _request_set(_records(before_dir, T_START))
    # Nothing is written out any more: every template now comes from the registry.
    assert {r["source"] for r in after_records} <= {"registry", "named-follow-up"}


def test_the_registry_wins_over_a_written_out_copy(out_dir, monkeypatch):
    """A written-out template is used only for a role the registry lacks."""
    container = QUERY_REGISTRY["container_state"]
    drifted = 'container_memory_swap{job="<job>",name!="",drifted="1"}'
    monkeypatch.setitem(
        query_registry._REGISTRY,
        "container_state",
        dataclasses.replace(
            container, expressions={**container.expressions, "swap": drifted}
        ),
    )
    _, manifests = _run(out_dir)
    swap = [
        r for r in _records(out_dir, T_START)
        if r["query"] == "container_state" and r["role"] == "swap"
    ]
    assert len(swap) == 2
    assert all(r["source"] == "registry" for r in swap)
    assert all('drifted="1"' in r["expression"] for r in swap)
    assert manifests[0]["request_count"] == STATIC_REQUESTS_PER_T + 3


# --- Named-container follow-ups -------------------------------------------


def test_named_followups_use_each_T_s_own_container_names(out_dir):
    def names(params):
        if params.get("time") == str(T_START):
            return {"cadvisor-pi5": ("example-a",), "cadvisor-vps": ("example-c",)}
        return {"cadvisor-pi5": ("example-b", "example-d"), "cadvisor-vps": ()}

    _run(out_dir, FakePrometheus(names_by_job=names), at=(T_START, T_END))
    for t, want in (
        (T_START, {("rp5", "example-a"), ("vps", "example-c")}),
        (T_END, {("rp5", "example-b"), ("rp5", "example-d")}),
    ):
        followups = [
            r for r in _records(out_dir, t) if r["source"] == "named-follow-up"
        ]
        got = {(r["arguments"]["node"], r["arguments"]["container"]) for r in followups}
        assert got == want
        for record in followups:
            node, name = record["arguments"]["node"], record["arguments"]["container"]
            assert record["query"] == "container_state"
            assert record["role"] == "named_container"
            assert record["expression"] == named_container_expression(
                node, name, known=[name]
            )
            assert record["request"]["params"]["time"] == t


def test_names_come_from_every_container_state_role(out_dir):
    """A name seen only in one role (say, `swap`) still gets its follow-up."""
    def names(params):
        if "container_memory_swap" in params.get("query", ""):
            return {"cadvisor-vps": ("example-only-in-swap",)}
        return {}

    _run(out_dir, FakePrometheus(names_by_job=names))
    followups = [r for r in _records(out_dir, T_START) if r["source"] == "named-follow-up"]
    assert [(r["arguments"]["node"], r["arguments"]["container"]) for r in followups] == [
        ("vps", "example-only-in-swap")
    ]


# --- What each saved response records -------------------------------------


def test_each_saved_response_records_its_request_and_answer(out_dir):
    _run(out_dir, max_points=60)
    records = _records(out_dir, T_START)
    assert len(records) == STATIC_REQUESTS_PER_T + 3
    for record in records:
        assert record["schema"] == capture.RECORD_SCHEMA
        assert record["T"] == T_START
        assert record["max_points"] == 60
        assert record["http_status"] == 200
        assert record["body"]["status"] == "success"
        assert record["request"]["params"]["query"] == record["expression"]
        if record["kind"] == "range":
            window = record["arguments"]["window"]
            assert record["step"] == range_step_seconds(window, 60)
            assert record["request"]["params"]["step"] == record["step"]
            assert record["request"]["params"]["end"] == T_START
        else:
            assert record["kind"] == "instant"
            assert record["step"] is None
            assert record["request"]["params"]["time"] == T_START


def test_a_non_200_answer_is_recorded_and_the_capture_continues(out_dir):
    fake = FakePrometheus(status_for=lambda q: 400 if q == "up" else 200)
    _, manifests = _run(out_dir, fake)
    records = _records(out_dir, T_START)
    bad = [r for r in records if r["expression"] == "up"]
    assert len(bad) == 1 and bad[0]["http_status"] == 400
    assert bad[0]["body"]["status"] == "error"
    assert len(records) == STATIC_REQUESTS_PER_T + 3
    assert manifests[0]["status_counts"] == {"200": STATIC_REQUESTS_PER_T + 2, "400": 1}


def test_a_transport_failure_is_recorded_not_raised(out_dir):
    def handler(request):
        if _params(request).get("query") == "up":
            raise httpx.ConnectError("refused", request=request)
        return FakePrometheus()(request)

    _, manifests = _run(out_dir, handler)
    bad = [r for r in _records(out_dir, T_START) if r["expression"] == "up"]
    assert bad[0]["http_status"] is None
    assert bad[0]["error"] == "ConnectError"
    assert "body" not in bad[0] or bad[0]["body"] is None
    assert manifests[0]["status_counts"]["error"] == 1


def test_one_private_subdirectory_per_T_with_a_manifest(out_dir):
    _, manifests = _run(out_dir, at=(T_START, T_MID, T_END))
    labels = sorted(p.name for p in out_dir.iterdir())
    assert labels == sorted(capture.time_label(t) for t in (T_START, T_MID, T_END))
    assert capture.time_label(T_START) == "20260923T062958Z"
    for label, manifest in zip(labels, sorted(manifests, key=lambda m: m["T"])):
        directory = out_dir / label
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        files = list(directory.iterdir())
        assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)
        on_disk = json.loads((directory / capture.MANIFEST_NAME).read_text())
        assert on_disk == manifest
        assert len(files) == manifest["request_count"] + 1


def test_an_existing_T_subdirectory_is_never_overwritten(out_dir):
    (out_dir / capture.time_label(T_START)).mkdir(mode=0o700)
    with pytest.raises(capture.CaptureRefused):
        _run(out_dir)


# --- The capture refuses an unsafe output path ----------------------------


def _refuses(path: Path):
    handler = FakePrometheus()
    with pytest.raises(capture.CaptureRefused):
        capture.run_capture(
            _client(handler),
            prometheus_url=PROMETHEUS,
            evaluation_times=(T_START,),
            max_points=60,
            out_dir=path,
        )
    assert handler.requests == [], "an unsafe path was refused only after querying"


def test_a_missing_output_path_is_refused(tmp_path):
    _refuses(tmp_path / "absent")


def test_a_file_is_not_an_output_directory(tmp_path):
    path = tmp_path / "file"
    path.write_text("")
    path.chmod(0o700)
    _refuses(path)


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o701, 0o770, 0o1700])
def test_any_mode_but_700_is_refused(tmp_path, mode):
    path = tmp_path / "scratch"
    path.mkdir()
    path.chmod(mode)
    _refuses(path)


def test_a_symlink_to_a_private_directory_is_refused(tmp_path, out_dir):
    link = tmp_path / "link"
    link.symlink_to(out_dir)
    _refuses(link)


def test_a_directory_owned_by_someone_else_is_refused(out_dir, monkeypatch):
    real_uid = os.getuid()
    monkeypatch.setattr(capture.os, "getuid", lambda: real_uid + 1)
    _refuses(out_dir)


def test_max_points_below_two_is_refused(out_dir):
    with pytest.raises(capture.CaptureRefused):
        _run(out_dir, max_points=1)


def test_a_repeated_T_is_refused_before_any_request(out_dir):
    handler = FakePrometheus()
    with pytest.raises(capture.CaptureRefused):
        _run(out_dir, handler, at=(T_START, T_MID, T_START))
    assert handler.requests == []
    assert list(out_dir.iterdir()) == []


def test_d3_domain_before_the_d3_roles_issues_nothing_for_rp2(out_dir, monkeypatch):
    """Group 3 may widen the domain to rp2 before it adds the roles.

    rp2 is then in domain and not derivable, so the written-out roles must skip
    it exactly as the registry's own roles do, and the count stays 155.
    """
    container = QUERY_REGISTRY["container_state"]
    monkeypatch.setitem(
        query_registry._REGISTRY,
        "container_state",
        dataclasses.replace(
            container,
            parameters=(QueryParameter("node", ("rp5", "vps", "rp2"), "Which node."),),
            unavailable=container.unavailable
            + (Unavailable(parameters={"node": "rp2"}, aspect=None, reason="no cadvisor."),),
        ),
    )
    _, manifests = _run(out_dir)
    assert manifests[0]["request_count"] == STATIC_REQUESTS_PER_T + 3
    assert not [
        r for r in _records(out_dir, T_START)
        if r["query"] == "container_state" and r["arguments"].get("node") == "rp2"
    ]


# --- Review round 1: incomplete follow-ups, repo-internal output, the CLI --


def test_the_manifest_says_whether_each_node_s_follow_ups_are_complete(out_dir):
    """A failed `container_state` answer shrinks the name set; the manifest says so."""
    fake = FakePrometheus(
        status_for=lambda q: 503 if q == 'container_memory_swap{job="cadvisor-vps",name!=""}' else 200
    )
    _, manifests = _run(out_dir, fake)
    followups = manifests[0]["named_followups"]
    assert followups["rp5"] == {"complete": True, "failed_roles": [], "names": 2}
    assert followups["vps"] == {"complete": False, "failed_roles": ["swap"], "names": 1}


def test_all_follow_ups_complete_on_a_clean_run(out_dir):
    _, manifests = _run(out_dir)
    assert all(v["complete"] for v in manifests[0]["named_followups"].values())
    assert set(manifests[0]["named_followups"]) == {"rp5", "vps"}


def test_an_output_path_inside_the_repository_is_refused(tmp_path, monkeypatch):
    """The output holds tailnet addresses; it must never land in the work tree."""
    repo = tmp_path / "repo"
    inside = repo / "scratch"
    inside.mkdir(parents=True)
    inside.chmod(0o700)
    monkeypatch.setattr(capture, "REPO_ROOT", repo)
    _refuses(inside)


def test_the_cli_parses_times_and_passes_max_points(out_dir, monkeypatch, capsys):
    fake = FakePrometheus()
    client = _client(fake)
    monkeypatch.setattr(capture.httpx, "Client", lambda: client)
    code = capture.main([
        "--prometheus-url", PROMETHEUS,
        "--max-points", "30",
        "--out", str(out_dir),
        "--at", str(T_START),
        "--at", "2026-09-23T06:30:26Z",
    ])
    assert code == 0
    assert sorted(p.name for p in out_dir.iterdir()) == [
        capture.time_label(T_START), capture.time_label(T_END)
    ]
    assert {r["max_points"] for r in _records(out_dir, T_END)} == {30}
    assert "158 requests" in capsys.readouterr().out


def test_the_cli_exits_2_on_a_refusal(tmp_path, capsys):
    code = capture.main([
        "--prometheus-url", PROMETHEUS, "--max-points", "60",
        "--out", str(tmp_path / "absent"), "--at", str(T_START),
    ])
    assert code == 2
    assert "capture refused" in capsys.readouterr().err


def test_a_zoneless_iso_time_is_rejected(out_dir):
    with pytest.raises(SystemExit):
        capture.main([
            "--prometheus-url", PROMETHEUS, "--max-points", "60",
            "--out", str(out_dir), "--at", "2026-09-23T06:30:26",
        ])
