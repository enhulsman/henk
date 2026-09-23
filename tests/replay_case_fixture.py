"""The committed placeholder fixture of the 2026-09-23 case, and its generator
(triage-quality task 12.4).

``tests/fixtures/replay/case-2026-09-23/`` has the same shape as the real case on
rp5, and none of its data (standing rule 1):

- ``inputs/henk-events.jsonl``: the preserved-event stand-in, ntfy frames as the
  ``henk-events`` cache holds them;
- ``inputs/audit-records.jsonl``: the audit-record stand-in, v4 records with tool
  **names** only;
- ``inputs/henk-handoffs.jsonl``: the original-handoff stand-in, the
  ``henk-handoffs`` cache's frames;
- ``inputs/reference.json``: the paraphrased reference, in the form the owner
  writes for ``rebuild --reference``;
- ``capture/<YYYYMMDDTHHMMSSZ>/``: captured-response files for the three ``T``
  values of ``notes/evidence-probe.md`` (1b.3), written by the real
  ``capture.run_capture`` against a placeholder Prometheus, then cut down to the
  vps swap story. The real capture holds all 195 requests per ``T``; this one
  holds a subset, with its manifest rewritten to list exactly the files kept. The
  nine D3/D4/D5 roles say ``source: "written-out"``, as the rp5 capture does.

Placeholders only: ``host-a.example``, ``example-a.service``, RFC 5737
addresses. Regenerate the capture after a registry template change with
``python -m tests.replay_case_fixture`` from the repository root; every other
file is hand-written.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from henk.replay import capture

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "replay" / "case-2026-09-23"
INPUTS_DIR = FIXTURE_DIR / "inputs"
CAPTURE_DIR = FIXTURE_DIR / "capture"

EVENTS_FILE = INPUTS_DIR / "henk-events.jsonl"
AUDIT_FILE = INPUTS_DIR / "audit-records.jsonl"
HANDOFFS_FILE = INPUTS_DIR / "henk-handoffs.jsonl"
REFERENCE_FILE = INPUTS_DIR / "reference.json"

#: notes/evidence-probe.md, 1b.3 (times only).
NOTIFIED = 1790144878
TRIAGE_AT = 1790145026
T_VALUES = (1790144998, 1790145012, 1790145026)
INTERVAL = (NOTIFIED + 120, TRIAGE_AT)
CASE_IDS = tuple(
    f"2026-09-23-swap-T{capture.time_label(t).split('T', 1)[1]}" for t in T_VALUES
)

FIRING_ID = "fxSwapFire01"
HANDOFF_ID = "fxHandoff001"

#: The roles the rp5 capture sent from written-out copies (group 1b), because
#: the registry did not have them yet.
WRITTEN_OUT_ROLES = {
    ("container_state", "memory_working_set"),
    ("container_state", "swap"),
    ("container_state", "restarts_15m"),
    ("container_state", "restarts_24h"),
    ("memory_movers", "movers_max"),
    ("memory_movers", "movers_min"),
    ("memory_movers", "movers_series"),
    ("host_service_state", "bad_states"),
    ("host_service_state", "unit_count"),
}

#: 06:12:00Z on 2026-09-23: the page-cache burst.
BURST = 1790143920
MIB = 1024 * 1024


def _kept(record: dict) -> bool:
    """The vps swap story: what a triage of this alert would plausibly ask."""
    query, role, args = record["query"], record["role"], record["arguments"]
    if query == "scrape_targets":
        return True
    if args.get("node") != "vps":
        return False
    if query == "node_resource_trend":
        return args["resource"] in ("swap_used", "swap_io", "memory") and \
            args["window"] in ("15m", "1h")
    if query in ("memory_movers", "host_service_state"):
        return args["window"] == "1h"
    return query == "container_state"


# --- The placeholder Prometheus ---------------------------------------------------


def _swap_used(ts: int) -> float:
    return 70.4 if ts < BURST else 98.39


def _swap_io(ts: int) -> float:
    if BURST <= ts < BURST + 120:
        return 1570.0
    if BURST + 900 <= ts:  # the later swap-in, pages touched again
        return 310.0
    return 2.0


def _memory(ts: int) -> float:
    return 61.0 if ts < BURST else 88.0


def _cgroup_ws(cgroup: str, ts: int) -> float:
    """Working set: the page-cache burst lands on example-a.service at 06:12."""
    if cgroup == "example-a.service":
        return (107 if ts < BURST else 1026) * MIB
    if cgroup in ("example-web", "example-worker"):
        return 210 * MIB
    return 48 * MIB


UNITS = ("example-a.service", "example-b.service")
CONTAINERS = ("example-web", "example-worker")


def _vector(result: list) -> httpx.Response:
    return httpx.Response(200, json={"status": "success",
                                     "data": {"resultType": "vector", "result": result}})


def _matrix(result: list) -> httpx.Response:
    return httpx.Response(200, json={"status": "success",
                                     "data": {"resultType": "matrix", "result": result}})


def _num(value: float) -> str:
    """Prometheus's sample string: exact, never rounded to six digits."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _range_values(params: dict, fn) -> list:
    start, end, step = int(params["start"]), int(params["end"]), int(params["step"])
    return [[ts, _num(fn(ts))] for ts in range(start, end + 1, step)]


def placeholder_prometheus(request: httpx.Request) -> httpx.Response:
    params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
    query = params.get("query", "")
    ranged = request.url.path.endswith("query_range")
    t = int(params.get("time", params.get("end", 0)))
    if query in ("up", "max_over_time(up[24h])"):
        return _vector([
            {"metric": {"__name__": "up", "job": "node-exporter-vps",
                        "instance": "192.0.2.20:9100"}, "value": [t, "1"]},
            {"metric": {"__name__": "up", "job": "cadvisor-vps",
                        "instance": "192.0.2.21:8080"}, "value": [t, "1"]},
        ])
    node = {"job": "node-exporter-vps", "instance": "192.0.2.20:9100"}
    if ranged and "SwapFree" in query:
        return _matrix([{"metric": node, "values": _range_values(params, _swap_used)}])
    if ranged and "pswpin" in query:
        return _matrix([{"metric": node, "values": _range_values(params, _swap_io)}])
    if ranged and "MemAvailable" in query:
        return _matrix([{"metric": node, "values": _range_values(params, _memory)}])
    if "/system" in query and "slice" in query:  # memory_movers: units, then containers
        cadvisor = {"job": "cadvisor-vps", "instance": "192.0.2.21:8080"}
        cgroups = [({**cadvisor, "id": f"/system.slice/{unit}"}, unit) for unit in UNITS]
        cgroups += [({**cadvisor, "name": name}, name) for name in CONTAINERS]
        series = []
        for metric, which in cgroups:
            if ranged:
                series.append({"metric": metric, "values": _range_values(
                    params, lambda ts, w=which: _cgroup_ws(w, ts))})
            elif query.startswith("max_over_time"):
                series.append({"metric": metric, "value": [t, _num(_cgroup_ws(which, BURST))]})
            else:
                series.append({"metric": metric, "value": [t, _num(_cgroup_ws(which, 0))]})
        return (_matrix if ranged else _vector)(series)
    if "node_systemd_unit_state" in query:
        if ranged:
            return _matrix([])
        return _vector([{"metric": {}, "value": [t, "180"]}])
    if 'job="cadvisor-vps",name!=""' in query:
        values = {
            "container_last_seen": lambda name: t,
            "container_start_time_seconds": lambda name: t - 12 * 86400,
            "container_memory_working_set_bytes": lambda name: 210 * MIB,
            "container_memory_swap": lambda name: (96 if name == "example-web" else 12) * MIB,
            "container_oom_events_total": lambda name: 0,
            "resets(": lambda name: 0,
        }
        for needle, fn in values.items():
            if needle in query:
                return _vector([
                    {"metric": {"job": "cadvisor-vps", "name": name}, "value": [t, _num(fn(name))]}
                    for name in CONTAINERS
                ])
        return _vector([])  # health_state: no container declares a healthcheck
    if 'job="cadvisor-vps",name="' in query:
        return _vector([{"metric": {}, "value": [t, _num(t)]}])
    return (_matrix if ranged else _vector)([])


# --- Regenerating the capture -----------------------------------------------------


def regenerate_capture(target: Path = CAPTURE_DIR, *, max_points: int = 60) -> None:
    """Run the real capture into a private scratch directory, keep the subset,
    and write it to ``target``. The capture refuses a directory inside the
    checkout, so it never writes there itself."""
    scratch = Path(tempfile.mkdtemp(prefix="henk-fixture-capture-"))
    try:
        scratch.chmod(0o700)
        capture.run_capture(
            httpx.Client(transport=httpx.MockTransport(placeholder_prometheus)),
            prometheus_url="http://192.0.2.10:9090",
            evaluation_times=T_VALUES,
            max_points=max_points,
            out_dir=scratch,
        )
        if target.exists():
            shutil.rmtree(target)
        for t in T_VALUES:
            label = capture.time_label(t)
            source, dest = scratch / label, target / label
            dest.mkdir(parents=True)
            manifest = json.loads((source / capture.MANIFEST_NAME).read_text())
            kept_files = []
            for entry in manifest["files"]:
                record = json.loads((source / entry["file"]).read_text())
                if not _kept(record):
                    continue
                if (record["query"], record["role"]) in WRITTEN_OUT_ROLES:
                    record["source"] = entry["source"] = "written-out"
                (dest / entry["file"]).write_text(
                    json.dumps(record, indent=1, sort_keys=True) + "\n")
                kept_files.append(entry)
            manifest["files"] = kept_files
            manifest["request_count"] = len(kept_files)
            manifest["status_counts"] = {"200": len(kept_files)}
            per_query: dict[str, int] = {}
            for entry in kept_files:
                bucket = ("container_state_named" if entry["source"] == "named-follow-up"
                          else entry["query"])
                per_query[bucket] = per_query.get(bucket, 0) + 1
            manifest["per_query"] = per_query
            (dest / capture.MANIFEST_NAME).write_text(
                json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover - a maintenance tool
    regenerate_capture()
    print(f"regenerated {CAPTURE_DIR}")
