"""Rebuilding a reconstructed reference case from preserved material (design D15).

The 2026-09-23 ``HenkSwapPressure`` triage ran before the recorder existed, so no
recording holds what it saw. ``python -m henk.replay rebuild`` builds one
reference case per captured evaluation time ``T`` instead, each as
``triage-cases/<prefix>-T<HHMMSSZ>/case.json`` in the layout
:mod:`henk.replay.case` reads, from four inputs the owner names by path:

1. **The preserved ntfy event.** The frames of the ``henk-events`` cache that the
   triage record names (by ``event_id``), composed by the current composer with
   **no recall block and no digest**, and the current tool names, as the live
   core composes an event turn. Memory or the handoff archive could carry the
   answer, so neither is consulted.
2. **The group 1b capture.** Its per-``T`` directories are referenced in place,
   never copied, so the case's ``capture.directory`` always resolves inside
   ``triage-cases/``. A capture elsewhere is refused. Nothing is rendered here:
   :class:`~henk.replay.case.ReconstructedQueries` renders each answer through
   the current renderers when a replay asks for it.
3. **The triage's audit record.** Its tool **names**, in order, become the
   original call sequence, with arguments ``unknown`` and no results; the v4
   record keeps nothing else (``audit-record.v4.schema.json``).
4. **The original handoff.** The ``henk-handoffs`` frame the record's
   ``handoff_message_id`` names, without its ``[AI]`` label, plus the record's
   diagnosis and confidence, is the case's ``original_candidate``.

The **reference** is the owner's verified ground truth, read from a JSON file
the owner writes, and checked against the recording schema's ``reference``
definition (``branch``, ``culprit``, ``mechanism``, ``fix``, optional ``notes``).

**Timing.** The case's ``interval`` runs from the notification time plus the
configured debounce to the triage record's ``at`` (evidence-probe 1b.3). A
captured ``T`` outside it is refused: that capture belongs to another incident.

**Drift, before anything is written.** Every captured answer is checked with
:func:`~henk.replay.case.capture_drift` against the current registry and
configuration: the expression, the ``kind``, ``T``, and for a range answer the
``max_points`` and step (the plan's window is filled from ``T`` through
``with_range_end``, as the live tool does from its clock). Mismatches are
recorded in the case's ``drift`` and printed; a replay will not serve them.

**Each case states what it grades.** ``grades: "current-renderers"`` and the
case's ``statement``: its evidence is the current renderers' output over a
re-capture, not the evidence the original triage saw.

**It spends nothing.** No model, no session, no network: the tool definitions
that name the framing's tools are built over the refusing transport. Every
check runs before the first write, so a refusal writes no case; the reference
case bound (:data:`~henk.replay.recorder.MAX_REFERENCE_CASES`) is checked for
the whole batch, and no case is ever evicted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx
import jsonschema

from henk.agent.triage import compose_event_turn_content, incident_times
from henk.agent.turns import EventTurn, EventTurnItem
from henk.events.identity import derive_identity
from henk.events.types import Event
from henk.replay import capture
from henk.replay.case import CASE_SCHEMA, CaptureIndex, ReplayCase, capture_drift
from henk.replay.harness import RefusingTransport, build_definition_registry
from henk.replay.recorder import (
    _CASE_ID_RE,
    MAX_REFERENCE_CASES,
    RECORDING_SCHEMA_PATH,
    CaseRefused,
    add_case,
    build_recording,
    list_cases,
)
from henk.replay.run import ReplayRefused
from henk.store.handoffs import parse_handoff_message_id
from henk.tools.notify import AI_LABEL

#: What every rebuilt case grades (design D15, "Stated plainly").
GRADES_CURRENT_RENDERERS = "current-renderers"

_T_DIR = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
_ENDINGS = ("completed", "error", "refused", "no-reply")
_PROFILES = ("chat", "event")


class RebuildRefused(ReplayRefused):
    """The rebuild will not write; no case was written."""


@dataclass(frozen=True)
class RebuildInputs:
    events: Path
    audit_records: Path
    capture: Path
    reference: Path
    case_prefix: str
    handoffs: Path | None = None
    handoff_document: Path | None = None
    event_id: str | None = None


@dataclass(frozen=True)
class RebuiltCase:
    case_id: str
    t: int
    path: Path | None
    drift: tuple
    case: dict


def case_id_for(prefix: str, t: int) -> str:
    """``<prefix>-T<HHMMSSZ>``, for example ``2026-09-23-swap-T062958Z``."""
    return f"{prefix}-T{capture.time_label(t).split('T', 1)[1]}"


# --- Reading the inputs -------------------------------------------------------------


def _rows(path: Path, what: str) -> list[dict[str, Any]]:
    """A JSON array, a single JSON object, or JSON Lines; objects only."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RebuildRefused(f"the {what} file {path} cannot be read "
                             f"({type(exc).__name__})") from None
    try:
        whole = json.loads(text)
    except ValueError:
        rows = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                raise RebuildRefused(
                    f"the {what} file {path} is not JSON or JSON Lines (line {number})"
                ) from None
    else:
        rows = whole if isinstance(whole, list) else [whole]
    return [row for row in rows if isinstance(row, dict)]


def _messages(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """ntfy ``message`` frames by id, first one kept (ids are unique in ntfy)."""
    frames: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("event") == "message" and isinstance(row.get("id"), str):
            frames.setdefault(row["id"], dict(row))
    return frames


def _iso(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


def _select_triage(rows: Sequence[Mapping[str, Any]], event_id: str | None) -> dict[str, Any]:
    triages = [
        dict(r) for r in rows
        if r.get("record_type") == "session" and r.get("trigger") == "event"
        and isinstance(r.get("event"), list) and r["event"]
    ]
    if event_id is not None:
        triages = [r for r in triages
                   if any(isinstance(e, dict) and e.get("event_id") == event_id
                          for e in r["event"])]
    if len(triages) == 1:
        return triages[0]
    if not triages:
        named = f" naming event {event_id!r}" if event_id is not None else ""
        raise RebuildRefused(f"the audit records hold no event triage{named}")
    listing = "; ".join(
        f"at {_iso(r.get('at'))}: event ids "
        + ", ".join(str(e.get("event_id")) for e in r["event"] if isinstance(e, dict))
        for r in triages
    )
    raise RebuildRefused(
        f"the audit records hold {len(triages)} event triages ({listing}); pass "
        "--event-id to choose one"
    )


def _reference(path: Path) -> dict[str, Any]:
    try:
        reference = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RebuildRefused(f"the reference file {path} cannot be read as JSON "
                             f"({type(exc).__name__})") from None
    schema = json.loads(RECORDING_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(reference, {"$ref": "#/definitions/reference",
                                        "definitions": schema["definitions"]})
    except jsonschema.ValidationError as exc:
        raise RebuildRefused(
            f"the reference file {path} is not a reference (branch, culprit, mechanism "
            f"and fix as non-empty strings, notes optional, nothing else): {exc.message}"
        ) from None
    return reference


def _unlabelled(text: str) -> str:
    prefix = f"{AI_LABEL} "
    return text[len(prefix):] if text.startswith(prefix) else text


def _original_document(triage: Mapping[str, Any], inputs: RebuildInputs) -> str | None:
    if inputs.handoff_document is not None:
        try:
            return _unlabelled(Path(inputs.handoff_document).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise RebuildRefused(f"the handoff document {inputs.handoff_document} cannot "
                                 f"be read ({type(exc).__name__})") from None
    # The record carries the tool's whole result string, not the bare id.
    handoff_id = parse_handoff_message_id(triage.get("handoff_message_id"))
    if not handoff_id:
        return None  # the triage published no handoff; there is nothing to find
    if inputs.handoffs is None:
        raise RebuildRefused(
            f"the triage published handoff {handoff_id}; pass --handoffs (the "
            "henk-handoffs cache) or --handoff-document"
        )
    frame = _messages(_rows(inputs.handoffs, "handoffs")).get(handoff_id)
    if frame is None:
        raise RebuildRefused(f"the handoffs cache holds no message {handoff_id}")
    if frame.get("attachment"):
        raise RebuildRefused(
            f"handoff {handoff_id} was delivered as an ntfy attachment, so its frame "
            "does not hold the document; save the attachment's text and pass it with "
            "--handoff-document"
        )
    message = frame.get("message")
    if not isinstance(message, str):
        raise RebuildRefused(f"handoff {handoff_id} holds no message text")
    return _unlabelled(message)


# --- The capture --------------------------------------------------------------------


def _strictly_inside(root: Path, candidate: Path) -> bool:
    return root.resolve() in candidate.resolve().parents


def _capture_times(root: Path) -> list[tuple[int, Path]]:
    found = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as exc:
        raise RebuildRefused(f"the capture {root} cannot be read "
                             f"({type(exc).__name__})") from None
    for entry in entries:
        if not _T_DIR.match(entry.name) or entry.is_symlink() or not entry.is_dir():
            continue
        directory = Path(entry.path)
        try:
            manifest = json.loads((directory / capture.MANIFEST_NAME).read_text(
                encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RebuildRefused(f"the capture manifest in {directory} cannot be read "
                                 f"({type(exc).__name__})") from None
        t = manifest.get("T") if isinstance(manifest, dict) else None
        if not isinstance(t, int) or isinstance(t, bool) or capture.time_label(t) != entry.name:
            raise RebuildRefused(f"the capture manifest in {directory} does not name "
                                 f"the time {entry.name}")
        found.append((t, directory))
    if not found:
        raise RebuildRefused(f"the capture {root} holds no captured T directories "
                             "(YYYYMMDDTHHMMSSZ, each with a manifest)")
    return sorted(found)


# --- Composing ----------------------------------------------------------------------


class _Ending:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.error_class = None
        self.http_status = None


def _tool_names(config: Any) -> frozenset[str]:
    """The live registry's names, built over the refusing transport: the same
    names the core hands the composer (``runtime.py`` ``registry.names()``)."""
    # Never used and holding no connection, so there is nothing to close.
    client = httpx.AsyncClient(transport=RefusingTransport())
    return frozenset(build_definition_registry(config, client).names())


def _turn(triage: Mapping[str, Any], frames: Mapping[str, Mapping[str, Any]]) -> EventTurn:
    items = []
    for entry in triage["event"]:
        event_id = entry.get("event_id") if isinstance(entry, dict) else None
        frame = frames.get(event_id) if isinstance(event_id, str) else None
        if frame is None:
            raise RebuildRefused(f"the events cache holds no message {event_id} that the "
                                 "triage record names")
        notified = frame.get("time")
        if type(notified) is not int or notified <= 0:
            raise RebuildRefused(f"event {event_id} carries no notification time")
        # The receive time is not preserved. The intake stamps it on arrival,
        # which the debounce start pins to the notification time (1b.3).
        event = Event(id=event_id, title=str(frame.get("title", "")),
                      message=str(frame.get("message", "")),
                      arrival_time=float(notified), raw=dict(frame))
        items.append(EventTurnItem(event=event, identity=derive_identity(event),
                                   recurrence=entry.get("recurrence") is True))
    announceable = triage.get("announceable")
    return EventTurn(items=tuple(items),
                     announceable=announceable if isinstance(announceable, bool) else True)


def _recording_id(at: float, case_id: str) -> str:
    """Deterministic, so a rebuild over the same inputs writes the same case."""
    stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{hashlib.sha256(case_id.encode()).hexdigest()[:8]}"


def _usage(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None

    def number(key: str) -> int | None:
        value = usage.get(key)
        return value if type(value) is int else None

    return {"input_tokens": number("input_tokens"), "output_tokens": number("output_tokens"),
            "cache_read_input_tokens": number("cache_read_input_tokens")}


def _recording(*, case_id: str, triage: Mapping[str, Any], turn: EventTurn, content: str,
               reference: Mapping[str, Any]) -> dict[str, Any]:
    at = triage.get("at")
    outcome = triage.get("outcome")
    if outcome not in _ENDINGS:
        raise RebuildRefused(f"the triage record's outcome {outcome!r} is not one of "
                             f"{', '.join(_ENDINGS)}")
    # A v4 record predates the event profile (group 9): its triage ran on the
    # only session factory there was, the chat one.
    profile = triage.get("profile") if triage.get("profile") in _PROFILES else "chat"
    effort = triage.get("effort") if isinstance(triage.get("effort"), str) else None
    model = triage.get("model") if isinstance(triage.get("model"), str) else None
    approvals = [a for a in triage.get("approvals") or () if isinstance(a, dict)]
    recording = build_recording(
        recording_id=_recording_id(float(at), case_id),
        at=float(at),
        turn=turn,
        content=content,
        reply=None,  # the owner's message was not preserved; see original_candidate
        ending=_Ending(outcome),
        fingerprint={"profile": profile, "model": model, "effort": effort,
                     "thinking": None, "system_prompt_sha256": None,
                     "tool_definitions_sha256": None},
        transcript=[],
        approvals=[{**a, "tier": a.get("tier") if isinstance(a.get("tier"), str) else None,
                    "reference": a.get("reference")
                    if isinstance(a.get("reference"), str) else None}
                   for a in approvals],
    )
    recording["reconstructed"] = True
    recording["transcript"] = [
        {"tool_use_id": None, "name": str(call["name"]), "arguments": "unknown",
         "result": None, "is_error": None}
        for call in triage.get("tool_calls") or ()
        if isinstance(call, dict) and isinstance(call.get("name"), str) and call["name"]
    ]
    recording["usage"] = _usage(triage.get("usage"))
    recording["reference"] = dict(reference)
    schema = json.loads(RECORDING_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(recording, schema)
    except jsonschema.ValidationError as exc:
        raise RebuildRefused(f"the rebuilt recording is not a valid recording: "
                             f"{exc.message}") from None
    return recording


# --- The rebuild --------------------------------------------------------------------


def plan_rebuild(config: Any, inputs: RebuildInputs, *,
                 clock: Callable[[], float] = time.time) -> list[RebuiltCase]:
    """Every case the inputs make, checked, with nothing written."""
    cases_dir = Path(config.audit.triage_cases_dir)
    capture_root = Path(inputs.capture)
    if not _strictly_inside(cases_dir, capture_root):
        raise RebuildRefused(
            f"the capture {capture_root} must lie inside {cases_dir}, because a case "
            "references its capture by a path relative to that directory"
        )
    if not capture_root.is_dir():
        raise RebuildRefused(f"the capture {capture_root} is not a directory")
    reference = _reference(inputs.reference)
    triage = _select_triage(_rows(inputs.audit_records, "audit records"), inputs.event_id)
    at = triage.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        raise RebuildRefused("the triage record carries no time (`at`)")
    turn = _turn(triage, _messages(_rows(inputs.events, "events")))
    document = _original_document(triage, inputs)
    times = _capture_times(capture_root)

    notified = min(incident_times(item.event).notified for item in turn.items)
    debounce = float(config.events.debounce_seconds)
    start, end = float(notified) + debounce, float(at)
    if start > end:
        raise RebuildRefused(
            f"the notification time plus the {debounce:g} s debounce ({_iso(start)}) "
            f"falls after the triage record ({_iso(end)})"
        )
    for t, directory in times:
        if not start <= t <= end:
            raise RebuildRefused(
                f"the capture time {directory.name} lies outside the triage's interval "
                f"{_iso(start)} to {_iso(end)}, so it is not this incident's capture"
            )

    content = compose_event_turn_content(turn, recall=None, tool_names=_tool_names(config),
                                         digest=None)
    max_points = config.homelab_query.query_range_max_points
    original = {
        "handoff_message_id": parse_handoff_message_id(triage.get("handoff_message_id")),
        "handoff_document": document,
        "diagnosis": triage.get("diagnosis"),
        "confidence": triage.get("confidence"),
        "triage_arc_complete": triage.get("triage_arc_complete"),
        "model": triage.get("model"),
        "at": float(at),
    }
    built_at = float(clock())
    planned = []
    for t, directory in times:
        case_id = case_id_for(inputs.case_prefix, t)
        if not _CASE_ID_RE.match(case_id) or ".." in case_id:
            raise RebuildRefused(f"--case-prefix {inputs.case_prefix!r} makes the invalid "
                                 f"case id {case_id!r}")
        index = CaptureIndex.load(directory)
        if not index.records:
            raise RebuildRefused(f"the capture {directory} holds no captured answers")
        drift = capture_drift(index, t=t, max_points=max_points)
        recording = _recording(case_id=case_id, triage=triage, turn=turn, content=content,
                               reference=reference)
        statement = ReplayCase(case_id=case_id, recording=recording, capture_dir=directory,
                               t=t, interval_start=start, interval_end=end).statement()
        case = {
            "schema": CASE_SCHEMA,
            "case_id": case_id,
            "recording": recording,
            "capture": {
                "directory": os.path.relpath(directory.resolve(), cases_dir.resolve()),
                "T": t,
                "interval": {"start": start, "end": end},
            },
            "drift": drift,
            "grades": GRADES_CURRENT_RENDERERS,
            "statement": statement,
            "original_candidate": original,
            "rebuild": {
                "at": built_at,
                "max_points": max_points,
                "debounce_seconds": debounce,
                "event_ids": [item.event.id for item in turn.items],
                "skipped_capture_files": index.skipped,
            },
        }
        planned.append(RebuiltCase(case_id=case_id, t=t, path=None, drift=tuple(drift),
                                   case=case))
    return planned


def rebuild(config: Any, inputs: RebuildInputs, *, replace: bool = False,
            clock: Callable[[], float] = time.time) -> list[RebuiltCase]:
    """Plan every case, check the batch against the bound, then write each."""
    planned = plan_rebuild(config, inputs, clock=clock)
    cases_dir = Path(config.audit.triage_cases_dir)
    existing = set(list_cases(cases_dir))
    new = 0
    for item in planned:
        if item.case_id in existing:
            if not replace:
                raise RebuildRefused(f"reference case {item.case_id!r} already exists; "
                                     "pass --replace to rebuild it")
        elif os.path.lexists(cases_dir / item.case_id):
            raise RebuildRefused(f"{item.case_id!r} exists and is not a reference case; "
                                 "it is left untouched")
        else:
            new += 1
    if len(existing) + new > MAX_REFERENCE_CASES:
        raise RebuildRefused(
            f"the reference case bound of {MAX_REFERENCE_CASES} would be passed: "
            f"{len(existing)} cases exist and this rebuild adds {new}. No case was "
            "written and none is evicted"
        )
    written = []
    for item in planned:
        try:
            path = add_case(cases_dir, item.case_id, item.case, replace=replace)
        except CaseRefused as exc:
            raise RebuildRefused(str(exc)) from None
        written.append(RebuiltCase(case_id=item.case_id, t=item.t, path=path,
                                   drift=item.drift, case=item.case))
    return written
