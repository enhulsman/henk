"""Reference cases, and serving a reconstructed case from its capture (D13, D15).

**The case layout** (read here, written by group 12b's ``rebuild``). A case is
the directory ``triage-cases/<case_id>/`` holding ``case.json``
(:data:`henk.replay.recorder.CASE_FILE`); only such directories are cases
(``list_cases``). ``case.json`` is a JSON object:

- ``schema``: :data:`CASE_SCHEMA`;
- ``case_id``: the directory's name;
- ``recording``: a whole ``henk.triage-recording.v1`` recording. It is wrapped,
  not extended, because that schema sets ``additionalProperties: false``. A
  reconstructed case has ``reconstructed: true``, its original call sequence as
  tool names with ``arguments: "unknown"``, null hashes, and optionally the
  owner's verified ``reference``;
- ``capture`` (required when reconstructed): ``directory``, the capture's per-``T``
  directory relative to ``triage-cases/`` (it must resolve inside it);
  ``T``, the evaluation time it was captured at; and ``interval``,
  ``{"start", "end"}``, the span in which the original triage could have queried,
  which is the case's stated timing uncertainty;
- ``drift`` (optional): the mismatches the rebuild listed.

A live recording kept as a reference case has no ``capture`` and is served its
recorded calls like any recording.

**A reconstructed case cannot carry memory or history.** Its composed content
must hold no recall block and no digest, and it names no prior handoffs; a case
that does is refused before any session exists (spec *The case cannot leak the
answer through memory or history*).

**Serving.** :class:`ReconstructedQueries` answers ``homelab_query`` for one
case from its capture (spec *Reconstructed cases are built from captured backend
data and say what they are*). It plans the call with the current registry, fills
the range window from ``T`` through ``with_range_end`` (as the live tool does
with its clock), looks every role's captured answer up by **(query, role,
arguments)**, never by file sequence number or ``source`` (the rp5 capture says
``written-out`` where new captures say ``registry``), and renders the answers
through the current renderer. A captured answer is served only when its
expression is byte-equal to the registry's current one for the same role and
arguments, its ``kind`` matches the current plan's, its ``T`` is the case's, and,
for a range answer, its ``max_points`` and step match the current configuration.
Anything else is "unavailable in reconstruction", naming the mismatch, and the
mismatch is listed in the run output. Gatus-backed queries and routes with no
historical form are unavailable; ``scrape_targets`` is rendered from ``up`` with
its targets part stated as unavailable.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from henk.agent.markers import PRIOR_HANDOFFS_HEADER, RECALL_BEGIN, RECALL_END_PREFIX
from henk.replay import capture
from henk.replay.recorder import CASE_FILE, RECORDING_SCHEMA, list_cases
from henk.tools.base import ToolResult
from henk.tools.query_registry import (
    QUERY_REGISTRY,
    QueryBackend,
    QueryOutcome,
    QueryRefused,
    plan_query,
    with_range_end,
)
from henk.tools.query_renderers import render_scrape_targets

logger = logging.getLogger("henk.replay.case")

CASE_SCHEMA = "henk.triage-case.v1"
UNAVAILABLE = "unavailable in reconstruction"

#: The one route a Prometheus-backed query reads that is stated rather than served.
_TARGETS_ROUTE = ("scrape_targets", "targets")

_Key = tuple[str, str, tuple[tuple[str, str], ...]]


class CaseInvalid(ValueError):
    """A case cannot be replayed as it stands; nothing was run."""


def iso(epoch: float | int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- The case -------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayCase:
    case_id: str
    recording: dict[str, Any]
    capture_dir: Path | None = None
    t: int | None = None
    interval_start: float | None = None
    interval_end: float | None = None
    listed_drift: tuple = ()

    @property
    def reconstructed(self) -> bool:
        return bool(self.recording.get("reconstructed"))

    def statement(self) -> str | None:
        """What a reconstructed case is, for its run output and terminal."""
        if not self.reconstructed:
            return None
        return (
            f"Reconstructed case: its evidence was re-captured from Prometheus at "
            f"{iso(self.t)}, not taken from the original triage. The original "
            f"triage's query times are unknown within {iso(self.interval_start)} to "
            f"{iso(self.interval_end)}, and that span is this case's timing "
            "uncertainty. This run grades the current renderers' evidence, not the "
            "evidence the original triage saw."
        )

    def describe(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "capture_time": self.t,
            "capture_time_iso": iso(self.t),
            "capture_interval": (
                {"start": self.interval_start, "end": self.interval_end,
                 "start_iso": iso(self.interval_start),
                 "end_iso": iso(self.interval_end)}
                if self.interval_start is not None else None
            ),
            "statement": self.statement(),
            "listed_drift": list(self.listed_drift),
        }


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and \
        math.isfinite(value)


def _check_no_memory_or_history(recording: Mapping[str, Any]) -> None:
    content = recording.get("content")
    if not isinstance(content, str):
        raise CaseInvalid("the case's recording has no composed content")
    for marker, what in ((RECALL_BEGIN, "a recall block"),
                         (RECALL_END_PREFIX, "a recall block"),
                         (PRIOR_HANDOFFS_HEADER, "a prior-handoffs digest")):
        if marker in content:
            raise CaseInvalid(
                f"the reconstructed case's content carries {what}; memory and "
                "history could carry the answer, so it is not replayed"
            )
    if recording.get("prior_handoff_ids"):
        raise CaseInvalid(
            "the reconstructed case names prior handoff ids; history could carry "
            "the answer, so it is not replayed"
        )


def _inside(root: Path, candidate: Path) -> bool:
    root = root.resolve()
    resolved = candidate.resolve()
    return resolved == root or root in resolved.parents


def load_case(cases_dir: str | Path, case_id: str) -> ReplayCase:
    """Read and check ``triage-cases/<case_id>/case.json``.

    Only an id ``list_cases`` reports is read, so no argument can name a path
    outside the directory, and raw or capture material is never a case.
    """
    root = Path(cases_dir)
    if case_id not in list_cases(root):
        raise CaseInvalid(f"no reference case {case_id!r} in {root}")
    try:
        case = json.loads((root / case_id / CASE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaseInvalid(f"case {case_id!r} cannot be read ({type(exc).__name__})") from None
    if not isinstance(case, dict) or case.get("schema") != CASE_SCHEMA:
        raise CaseInvalid(f"case {case_id!r} is not a {CASE_SCHEMA} case")
    recording = case.get("recording")
    if not isinstance(recording, dict) or recording.get("schema") != RECORDING_SCHEMA:
        raise CaseInvalid(f"case {case_id!r} does not wrap a {RECORDING_SCHEMA} recording")
    listed = case.get("drift") or ()
    if not recording.get("reconstructed"):
        return ReplayCase(case_id=case_id, recording=recording,
                          listed_drift=tuple(listed))
    _check_no_memory_or_history(recording)
    spec = case.get("capture")
    if not isinstance(spec, dict):
        raise CaseInvalid(f"reconstructed case {case_id!r} names no capture")
    directory, t, interval = spec.get("directory"), spec.get("T"), spec.get("interval")
    if not isinstance(directory, str) or not directory or os.path.isabs(directory):
        raise CaseInvalid(f"case {case_id!r}: capture.directory must be a relative path")
    if not isinstance(t, int) or isinstance(t, bool):
        raise CaseInvalid(f"case {case_id!r}: capture.T must be an integer epoch")
    if not (isinstance(interval, dict) and _number(interval.get("start"))
            and _number(interval.get("end"))):
        raise CaseInvalid(f"case {case_id!r}: capture.interval needs a start and an end")
    capture_dir = root / directory
    if not _inside(root, capture_dir):
        raise CaseInvalid(f"case {case_id!r}: its capture lies outside {root}")
    if not capture_dir.is_dir():
        raise CaseInvalid(f"case {case_id!r}: its capture directory does not exist")
    return ReplayCase(
        case_id=case_id,
        recording=recording,
        capture_dir=capture_dir,
        t=t,
        interval_start=float(interval["start"]),
        interval_end=float(interval["end"]),
        listed_drift=tuple(listed),
    )


# --- The capture ---------------------------------------------------------------


def _key(record: Mapping[str, Any]) -> _Key | None:
    query, role, arguments = record.get("query"), record.get("role"), record.get("arguments")
    if not (isinstance(query, str) and isinstance(role, str) and isinstance(arguments, dict)):
        return None
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in arguments.items()):
        return None
    return (query, role, tuple(sorted(arguments.items())))


@dataclass
class CaptureIndex:
    """One ``T``'s captured answers, keyed by (query, role, arguments)."""

    records: dict[_Key, dict[str, Any]] = field(default_factory=dict)
    duplicates: set = field(default_factory=set)
    skipped: int = 0

    @classmethod
    def load(cls, directory: str | Path) -> "CaptureIndex":
        index = cls()
        for path in sorted(Path(directory).glob("*.json")):
            if path.name == capture.MANIFEST_NAME:
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("skipping unreadable capture file %s (%s)", path.name,
                               type(exc).__name__)
                index.skipped += 1
                continue
            key = _key(record) if isinstance(record, dict) else None
            if key is None or record.get("schema") != capture.RECORD_SCHEMA:
                index.skipped += 1
                continue
            if record.get("role") == capture.NAMED_ROLE:
                continue  # a named follow-up; no tool call can ask for one today
            if key in index.records:
                index.duplicates.add(key)
            index.records[key] = record
        return index

    def get(self, key: _Key) -> dict[str, Any] | None:
        return None if key in self.duplicates else self.records.get(key)


def _mismatches(record: Mapping[str, Any], plan: Any, role: str, expression: str,
                *, t: int, max_points: int) -> list[str]:
    """How a captured answer differs from what the current registry would ask."""
    problems = []
    if record.get("expression") != expression:
        problems.append(
            f"the captured expression differs from the current registry "
            f"(captured {record.get('expression')!r}, registry {expression!r})"
        )
    kind = "range" if capture._is_range(plan.entry, role, plan.window) else "instant"
    if record.get("kind") != kind:
        problems.append(
            f"the captured kind differs (captured {record.get('kind')!r}, registry {kind!r})"
        )
    if record.get("T") != t:
        problems.append(f"the capture's T differs (captured {record.get('T')!r}, case {t})")
    if kind == "range":
        if record.get("max_points") != max_points:
            problems.append(
                f"the captured max_points differs (captured {record.get('max_points')!r}, "
                f"configured {max_points})"
            )
        step = plan.range_window.step if plan.range_window is not None else None
        if record.get("step") != step:
            problems.append(
                f"the captured step differs (captured {record.get('step')!r}, "
                f"registry {step!r})"
            )
    return problems


def _drift_entry(key: _Key, problems: list[str]) -> dict[str, Any]:
    return {"query": key[0], "role": key[1], "arguments": dict(key[2]),
            "mismatches": list(problems)}


def capture_drift(index: CaptureIndex, *, t: int, max_points: int) -> list[dict[str, Any]]:
    """Every captured answer the current registry would not be served, and why.

    The rebuild lists these in the case; a run lists them in its output.
    """
    drift = []
    for key in sorted(index.records):
        query, role, arguments = key
        if key in index.duplicates:
            drift.append(_drift_entry(key, ["the capture holds more than one answer"]))
            continue
        record = index.records[key]
        try:
            plan = plan_query(query, dict(arguments))
        except QueryRefused:
            drift.append(_drift_entry(key, ["the registry no longer accepts these arguments"]))
            continue
        if plan.outcome is not QueryOutcome.ANSWERED:
            drift.append(_drift_entry(key, ["the registry no longer answers this call"]))
            continue
        expression = plan.expressions.get(role)
        if expression is None:
            drift.append(_drift_entry(key, ["the registry no longer has this role"]))
            continue
        plan = with_range_end(plan, t, max_points)
        problems = _mismatches(record, plan, role, expression, t=t, max_points=max_points)
        if problems:
            drift.append(_drift_entry(key, problems))
    return drift


def _successful(record: Mapping[str, Any]) -> bool:
    body = record.get("body")
    return record.get("http_status") == 200 and isinstance(body, dict) and \
        body.get("status") == "success"


class ReconstructedQueries:
    """``homelab_query`` for one reconstructed case, answered from its capture."""

    def __init__(self, index: CaptureIndex, *, t: int, max_points: int) -> None:
        self._index = index
        self._t = t
        self._max_points = max_points
        #: Mismatches met while serving, one entry per refused call.
        self.drift: list[dict[str, Any]] = []

    def _unavailable(self, reason: str) -> ToolResult:
        return ToolResult.failure(f"{UNAVAILABLE}: {reason}")

    def serve(self, arguments: Mapping[str, Any]) -> ToolResult:
        arguments = dict(arguments)
        query_name = arguments.pop("query_name", None)
        entry = QUERY_REGISTRY.get(query_name) if isinstance(query_name, str) else None
        if entry is not None and (
            entry.backend is not QueryBackend.PROMETHEUS
            or any(p.discovered for p in entry.parameters)
        ):
            backend = "Gatus" if entry.backend is QueryBackend.GATUS else entry.backend.value
            return self._unavailable(
                f"{entry.name} reads {backend}, which has no historical form, so the "
                "case holds no answer for it. This is a limit of the reconstruction, "
                "not an answer."
            )
        try:
            plan = plan_query(query_name, arguments)
        except QueryRefused as refusal:
            return ToolResult.failure(str(refusal))
        if plan.outcome is QueryOutcome.NOT_DERIVABLE:
            return ToolResult.success(plan.message)
        # The live tool passes its clock here; a reconstructed case passes T.
        plan = with_range_end(plan, self._t, self._max_points)
        arguments_key = tuple(sorted(plan.arguments.items()))
        payloads: dict[str, Any] = {}
        problems: list[str] = []
        for role, expression in plan.expressions.items():
            key = (plan.entry.name, role, arguments_key)
            if key in self._index.duplicates:
                problems.append(f"the capture holds more than one answer for {role}")
                continue
            record = self._index.get(key)
            if record is None:
                problems.append(f"the capture holds no answer for {role}")
                continue
            mismatch = _mismatches(record, plan, role, expression, t=self._t,
                                   max_points=self._max_points)
            if mismatch:
                self.drift.append(_drift_entry(key, mismatch))
                problems.append(f"{role}: " + "; ".join(mismatch))
                continue
            if not _successful(record):
                status = record.get("http_status")
                problems.append(
                    f"the capture recorded no successful answer for {role} "
                    f"(HTTP {status if status is not None else 'none'})"
                )
                continue
            payloads[role] = record["body"]
        targets_unavailable = False
        for role in plan.routes:
            if (plan.entry.name, role) == _TARGETS_ROUTE:
                targets_unavailable = True
            else:
                problems.append(f"the {role} route has no historical form")
        if problems:
            return self._unavailable(f"{plan.entry.name}: " + "; ".join(problems))
        try:
            if targets_unavailable:
                text = render_scrape_targets(plan, payloads, targets_unavailable=True)
            else:
                text = plan.entry.renderer(plan, payloads)
        except NotImplementedError as exc:
            return ToolResult.failure(f"{plan.entry.name}: {exc}")
        except Exception:  # the live tool's own guard (homelab_query.py `_run`)
            logger.exception("rendering %s failed", plan.entry.name)
            return ToolResult.failure(
                f"{plan.entry.name}: the backend answered, but its response could "
                "not be summarised. No partial result is returned."
            )
        return ToolResult.success(text)
