"""Capture raw Prometheus answers at a chosen evaluation time (triage-quality D15).

The 2026-09-23 `HenkSwapPressure` case has no recording: the audit record keeps
tool **names** only, so the evidence the original triage saw is gone. This script
re-asks Prometheus every question `homelab_query` could have asked, pinned to an
evaluation time `T` inside the triage interval, and saves each raw answer. The
rebuild (`python -m henk.replay rebuild`) later renders those answers through the
current renderers.

`HomelabQueryTool` cannot do this itself. Its instant queries send no `time=`
(`henk/tools/homelab_query.py:293`), and its only clock seam is the range `end`
(`:299`). So the requests are built here from the registry's own templates, and
the tests pin that they cover the registry's whole argument space and that every
range request is the one the tool would have sent with its clock at `T`.

**Written-out templates.** Groups 3 and 4 add nine expression roles the registry
does not have yet: D3's container memory, D4's restarts, and D5's two new queries.
Their text is the D5 canonical table, byte for byte, and it appears here because
Prometheus retention (the 24h windows ending in the triage interval fall out on
2026-10-07 06:30Z) will not wait for those groups. A written-out template is used
only for a role the registry lacks, so once groups 3 and 4 land the capture reads
every template from the registry and the request count does not change. Tasks
3.9 and 4.4 then retire the copies below.

**What is never captured.** `endpoint_history` is Gatus-backed, and
`scrape_targets`' `/api/v1/targets` route takes no time parameter, so neither has
a historical form. Their absence is stated by the rebuild, not faked here.

The output holds tailnet addresses (Prometheus `instance` labels), so it is
written only into an existing mode-700 directory owned by the invoking user, and
it never enters the repository.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import httpx

from henk.tools import query_registry
from henk.tools.query_registry import (
    CADVISOR_JOBS,
    NODE_EXPORTER_JOBS,
    PROMETHEUS_WINDOWS,
    WINDOW_SECONDS,
    QueryBackend,
    QueryEntry,
    QueryOutcome,
    QueryRefused,
    named_container_expression,
    plan_query,
    range_step_seconds,
)

RECORD_SCHEMA = "henk.triage-capture.v1"
MANIFEST_NAME = "manifest.json"

INSTANT_ROUTE = "/api/v1/query"
RANGE_ROUTE = "/api/v1/query_range"

#: The per-name follow-up form of `container_state`, as a role.
NAMED_ROLE = "named_container"

#: The checkout this module lives in. The output holds tailnet addresses, so a
#: directory inside it is refused however private it is.
REPO_ROOT = Path(__file__).resolve().parents[2]


class CaptureRefused(RuntimeError):
    """The capture will not run: nothing was requested and nothing written."""


@dataclass(frozen=True)
class WrittenTemplate:
    """One row of the D5 canonical table."""

    query: str
    role: str
    kind: str  # "instant" | "range"
    template: str


@dataclass(frozen=True)
class WrittenDomain:
    """The argument space of a query the registry does not have yet."""

    nodes: tuple[str, ...]
    job_map: Mapping[str, str]
    unavailable: tuple[str, ...]
    windows: tuple[str, ...]


_UNITS = r'container_memory_working_set_bytes{job="<job>",id=~"/system\\.slice/.+\\.service"}'
_NAMED = 'container_memory_working_set_bytes{job="<job>",name!=""}'
_RESETS = 'max by (name) (resets(container_cpu_usage_seconds_total{job="<job>",name!=""}[<lookback>]))'

#: The D5 canonical table. Retired by tasks 3.9 and 4.4 once the registry's
#: expressions are pinned byte-equal to these.
WRITTEN_OUT_TEMPLATES: tuple[WrittenTemplate, ...] = (
    WrittenTemplate("container_state", "memory_working_set", "instant", _NAMED),
    WrittenTemplate(
        "container_state", "swap", "instant", 'container_memory_swap{job="<job>",name!=""}'
    ),
    WrittenTemplate(
        "container_state", "restarts_15m", "instant", _RESETS.replace("<lookback>", "15m")
    ),
    WrittenTemplate(
        "container_state", "restarts_24h", "instant", _RESETS.replace("<lookback>", "24h")
    ),
    WrittenTemplate(
        "memory_movers",
        "movers_max",
        "instant",
        f"max_over_time({_UNITS}[<window>]) or max_over_time({_NAMED}[<window>])",
    ),
    WrittenTemplate(
        "memory_movers",
        "movers_min",
        "instant",
        f"min_over_time({_UNITS}[<window>]) or min_over_time({_NAMED}[<window>])",
    ),
    WrittenTemplate("memory_movers", "movers_series", "range", f"{_UNITS} or {_NAMED}"),
    WrittenTemplate(
        "host_service_state",
        "bad_states",
        "range",
        'node_systemd_unit_state{job="<job>",state=~"activating|failed"} == 1',
    ),
    WrittenTemplate(
        "host_service_state",
        "unit_count",
        "instant",
        'count(node_systemd_unit_state{job="<job>"})',
    ),
)

#: The domains of D5's two new queries, as the design states them. rp2 runs no
#: cadvisor; only the vps node-exporter runs the systemd collector (measured
#: 2026-09-23). Used only while the registry has no entry of that name.
WRITTEN_OUT_DOMAINS: Mapping[str, WrittenDomain] = {
    "memory_movers": WrittenDomain(
        nodes=("rp5", "vps", "rp2"),
        job_map=CADVISOR_JOBS,
        unavailable=("rp2",),
        windows=PROMETHEUS_WINDOWS,
    ),
    "host_service_state": WrittenDomain(
        nodes=("rp5", "vps", "rp2"),
        job_map=NODE_EXPORTER_JOBS,
        unavailable=("rp5", "rp2"),
        windows=PROMETHEUS_WINDOWS,
    ),
}


@dataclass(frozen=True)
class CaptureRequest:
    query: str
    role: str
    arguments: tuple[tuple[str, str], ...]
    kind: str
    expression: str
    window: str | None
    source: str  # "registry" | "written-out" | "named-follow-up"

    @property
    def key(self) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        return (self.query, self.role, self.arguments)


# --- Planning: the closed argument space ----------------------------------


def _combinations(entry: QueryEntry) -> Iterator[dict[str, str]]:
    names = [p.name for p in entry.parameters]
    for values in itertools.product(*(p.domain or () for p in entry.parameters)):
        yield dict(zip(names, values))


def _is_range(entry: QueryEntry, role: str, window: str | None) -> bool:
    """The tool's own rule (`homelab_query.py` `_prometheus_request`)."""
    roles = entry.range_roles
    return entry.range_query and (roles is None or role in roles) and window is not None


def _fill(template: str, fields: Mapping[str, str]) -> str:
    filled = template
    for name, value in fields.items():
        filled = filled.replace(f"<{name}>", value)
    if "<" in filled and ">" in filled:
        raise CaptureRefused(f"internal error: unfilled placeholder in {template!r}")
    return filled


def _registry_requests(registry: Mapping[str, QueryEntry]) -> Iterator[CaptureRequest]:
    for name, entry in registry.items():
        if entry.backend is not QueryBackend.PROMETHEUS:
            continue
        if any(p.discovered for p in entry.parameters):
            continue
        for arguments in _combinations(entry):
            plan = plan_query(name, arguments)
            if plan.outcome is not QueryOutcome.ANSWERED:
                continue
            for role, expression in plan.expressions.items():
                yield CaptureRequest(
                    query=name,
                    role=role,
                    arguments=tuple(sorted(plan.arguments.items())),
                    kind="range" if _is_range(entry, role, plan.window) else "instant",
                    expression=expression,
                    window=plan.window,
                    source="registry",
                )


def _written_out_requests(registry: Mapping[str, QueryEntry]) -> Iterator[CaptureRequest]:
    for template in WRITTEN_OUT_TEMPLATES:
        entry = registry.get(template.query)
        if entry is not None:
            if template.role in entry.expressions:
                continue
            combinations = []
            for arguments in _combinations(entry):
                plan = plan_query(template.query, arguments)
                if plan.outcome is QueryOutcome.ANSWERED:
                    combinations.append((plan.arguments, entry.job_map or {}))
        else:
            domain = WRITTEN_OUT_DOMAINS[template.query]
            combinations = [
                ({"node": node, "window": window}, domain.job_map)
                for node in domain.nodes
                if node not in domain.unavailable
                for window in domain.windows
            ]
        for arguments, job_map in combinations:
            fields = {**arguments, "job": job_map[arguments["node"]]}
            window = arguments.get("window")
            yield CaptureRequest(
                query=template.query,
                role=template.role,
                arguments=tuple(sorted(arguments.items())),
                kind=template.kind if window is not None else "instant",
                expression=_fill(template.template, fields),
                window=window,
                source="written-out",
            )


def plan_requests() -> list[CaptureRequest]:
    """Every request one `T` issues before its named-container follow-ups.

    The union of the registry and the written-out templates, deduplicated by
    (query, role, arguments), with the registry first. A collision would mean a
    written-out copy slipped past the role check, which is a defect, not a
    tie to break quietly.
    """
    registry = query_registry.QUERY_REGISTRY
    requests: dict[tuple, CaptureRequest] = {}
    for request in itertools.chain(
        _registry_requests(registry), _written_out_requests(registry)
    ):
        if request.key in requests:
            raise CaptureRefused(f"internal error: two templates for {request.key}")
        requests[request.key] = request
    return list(requests.values())


def _named_followups(
    captured: Sequence[tuple[CaptureRequest, dict[str, Any]]],
) -> list[CaptureRequest]:
    """One follow-up per container name in this `T`'s own `container_state` answers."""
    names: dict[str, set[str]] = {}
    for request, record in captured:
        if request.query != "container_state" or request.source == "named-follow-up":
            continue
        node = dict(request.arguments)["node"]
        for series in _result(record):
            name = series.get("metric", {}).get("name") if isinstance(series, dict) else None
            if isinstance(name, str) and name:
                names.setdefault(node, set()).add(name)
    followups = []
    for node in sorted(names):
        known = sorted(names[node])
        for name in known:
            try:
                expression = named_container_expression(node, name, known=known)
            except QueryRefused:
                continue
            followups.append(
                CaptureRequest(
                    query="container_state",
                    role=NAMED_ROLE,
                    arguments=(("container", name), ("node", node)),
                    kind="instant",
                    expression=expression,
                    window=None,
                    source="named-follow-up",
                )
            )
    return followups


def _followup_completeness(
    captured: Sequence[tuple[CaptureRequest, dict[str, Any]]],
    followups: Sequence[CaptureRequest],
) -> dict[str, dict[str, Any]]:
    """Per node: were the names derived from a full set of answers?

    A failed `container_state` answer contributes no names, so its node's
    follow-ups may be missing some. The rebuild must not read that as "these
    were all the containers".
    """
    report: dict[str, dict[str, Any]] = {}
    for request, record in captured:
        if request.query != "container_state" or request.source == "named-follow-up":
            continue
        node = dict(request.arguments)["node"]
        entry = report.setdefault(node, {"complete": True, "failed_roles": [], "names": 0})
        body = record.get("body")
        if record.get("http_status") != 200 or not (
            isinstance(body, dict) and body.get("status") == "success"
        ):
            entry["complete"] = False
            entry["failed_roles"].append(request.role)
    for request in followups:
        report[dict(request.arguments)["node"]]["names"] += 1
    for entry in report.values():
        entry["failed_roles"].sort()
    return report


def _result(record: Mapping[str, Any]) -> list:
    body = record.get("body")
    if record.get("http_status") != 200 or not isinstance(body, dict):
        return []
    data = body.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    return result if isinstance(result, list) else []


# --- Issuing and saving ---------------------------------------------------


def _request_params(request: CaptureRequest, t: int, max_points: int) -> tuple[str, dict, int | None]:
    if request.kind == "range":
        step = range_step_seconds(request.window, max_points)
        span = WINDOW_SECONDS[request.window]
        return RANGE_ROUTE, {"query": request.expression, "start": t - span, "end": t, "step": step}, step
    return INSTANT_ROUTE, {"query": request.expression, "time": t}, None


def _issue(
    client: httpx.Client,
    prometheus_url: str,
    request: CaptureRequest,
    t: int,
    max_points: int,
    timeout: float,
) -> dict[str, Any]:
    route, params, step = _request_params(request, t, max_points)
    record: dict[str, Any] = {
        "schema": RECORD_SCHEMA,
        "T": t,
        "T_iso": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "query": request.query,
        "role": request.role,
        "arguments": dict(request.arguments),
        "kind": request.kind,
        "source": request.source,
        "expression": request.expression,
        "window": request.window,
        "max_points": max_points,
        "step": step,
        "request": {"path": route, "params": params},
    }
    try:
        response = client.get(f"{prometheus_url}{route}", params=params, timeout=timeout)
    except httpx.HTTPError as exc:
        record["http_status"] = None
        record["error"] = type(exc).__name__
        record["body"] = None
        return record
    record["http_status"] = response.status_code
    try:
        record["body"] = response.json()
    except ValueError:
        record["body"] = {"text": response.text}
    return record


def time_label(t: int) -> str:
    """`20260923T062958Z`: the per-`T` subdirectory name."""
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _slug(request: CaptureRequest) -> str:
    parts = [request.query, request.role] + [f"{k}={v}" for k, v in request.arguments]
    raw = "-".join(parts)
    return "".join(c if c.isalnum() or c in "._=-" else "_" for c in raw)[:160]


def _write_private(path: Path, payload: Any) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")


def check_output_dir(path: Path) -> None:
    """Refuse anything but an existing mode-700 directory owned by the invoker.

    `lstat`, so a symlink is refused rather than followed: a link pointing at a
    private directory today can be repointed tomorrow.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise CaptureRefused(f"output path {path} does not exist") from None
    if stat.S_ISLNK(info.st_mode):
        raise CaptureRefused(f"output path {path} is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise CaptureRefused(f"output path {path} is not a directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise CaptureRefused(
            f"output path {path} has mode {stat.S_IMODE(info.st_mode):o}, not 700"
        )
    if info.st_uid != os.getuid():
        raise CaptureRefused(f"output path {path} is not owned by the invoking user")
    resolved, repo = Path(path).resolve(), Path(REPO_ROOT).resolve()
    if resolved == repo or repo in resolved.parents:
        raise CaptureRefused(f"output path {path} is inside the repository checkout")


def run_capture(
    client: httpx.Client,
    *,
    prometheus_url: str,
    evaluation_times: Iterable[int],
    max_points: int,
    out_dir: Path,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Capture every `T` into `<out_dir>/<time_label(T)>/`; return the manifests."""
    out_dir = Path(out_dir)
    times = [int(t) for t in evaluation_times]
    if max_points < 2:
        raise CaptureRefused("max_points must be at least 2, as in henk/config.py")
    if len(set(times)) != len(times):
        raise CaptureRefused("an evaluation time is repeated; each T gets one directory")
    check_output_dir(out_dir)
    for t in times:
        if os.path.lexists(out_dir / time_label(t)):
            raise CaptureRefused(f"{out_dir / time_label(t)} already exists")
    prometheus_url = prometheus_url.rstrip("/")
    planned = plan_requests()

    manifests = []
    for t in times:
        directory = out_dir / time_label(t)
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        captured: list[tuple[CaptureRequest, dict[str, Any]]] = []
        for request in planned:
            captured.append((request, _issue(client, prometheus_url, request, t, max_points, timeout)))
        followups = _named_followups(captured)
        completeness = _followup_completeness(captured, followups)
        for request in followups:
            captured.append((request, _issue(client, prometheus_url, request, t, max_points, timeout)))

        files = []
        status_counts: dict[str, int] = {}
        per_query: dict[str, int] = {}
        for seq, (request, record) in enumerate(captured, start=1):
            name = f"{seq:04d}-{_slug(request)}.json"
            _write_private(directory / name, record)
            status = "error" if record["http_status"] is None else str(record["http_status"])
            status_counts[status] = status_counts.get(status, 0) + 1
            bucket = (
                "container_state_named" if request.source == "named-follow-up" else request.query
            )
            per_query[bucket] = per_query.get(bucket, 0) + 1
            files.append(
                {
                    "file": name,
                    "query": request.query,
                    "role": request.role,
                    "arguments": dict(request.arguments),
                    "source": request.source,
                    "http_status": record["http_status"],
                }
            )
        manifest = {
            "schema": RECORD_SCHEMA,
            "T": t,
            "T_iso": captured[0][1]["T_iso"] if captured else None,
            "max_points": max_points,
            "request_count": len(captured),
            "status_counts": status_counts,
            "per_query": per_query,
            "named_followups": completeness,
            "files": files,
        }
        _write_private(directory / MANIFEST_NAME, manifest)
        manifests.append(manifest)
    return manifests


# --- CLI ------------------------------------------------------------------


def _parse_time(value: str) -> int:
    if value.isdigit():
        return int(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r} carries no timezone")
    return int(parsed.timestamp())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m henk.replay.capture",
        description="Capture raw Prometheus answers for every homelab_query "
        "expression at fixed evaluation times.",
    )
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument(
        "--max-points",
        type=int,
        required=True,
        help="rp5's effective homelab_query.query_range_max_points (default 60 when absent)",
    )
    parser.add_argument("--out", type=Path, required=True, help="existing mode-700 directory")
    parser.add_argument(
        "--at", type=_parse_time, action="append", required=True,
        help="evaluation time, epoch seconds or ISO 8601 with a zone; repeatable",
    )
    args = parser.parse_args(argv)
    try:
        with httpx.Client() as client:
            manifests = run_capture(
                client,
                prometheus_url=args.prometheus_url,
                evaluation_times=args.at,
                max_points=args.max_points,
                out_dir=args.out,
            )
    except CaptureRefused as exc:
        print(f"capture refused: {exc}", file=sys.stderr)
        return 2
    for manifest in manifests:
        print(
            f"{time_label(manifest['T'])}: {manifest['request_count']} requests, "
            f"statuses {manifest['status_counts']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
