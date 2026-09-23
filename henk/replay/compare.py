"""``compare``: an original triage beside its replay runs (triage-quality D15).

A pure file read. It reads the source (a recording, or a reference case) and
run files under ``triage-replays/<source id>/``, and prints to the owner's
terminal. It builds no session, no transport, no channel and no store, and it
writes nothing.

For each candidate, the original and then each run, it shows:

- the model, effort and thinking;
- the ending, whether the arc is complete, and the stated confidence;
- the arc lines (``Diagnosis:``, ``Fix:``, ``Pickup:``) as the reply wrote them;
- the start of the handoff, bounded at :data:`HANDOFF_START_CHARS`;
- the tool calls in order, each tagged with how the replay answered it, and a
  call the recording could not answer marked ``NOT RECORDED``;
- token usage.

Every text that came from a model or a payload is stripped of control
characters before it is printed, so a reply cannot move the owner's cursor or
recolour the terminal.

:func:`load_runs` is shared with ``grade``: a run id must name a run file of the
same source, and the ``grades/`` subdirectory is never read as a run.

**The original candidate** (:func:`original_of`, shared with ``grade``) is the
recording's reply and the handoff documents its transcript holds. A rebuilt
case's recording has neither: its reply was not preserved (``rebuild`` writes
``reply: null``) and its calls carry ``arguments: "unknown"``. Its original is
then the case's ``original_candidate``: the original handoff, and the audit
record's diagnosis and confidence. ``compare`` shows those, and says the reply
was not preserved; ``grade`` shows the judge :data:`REPLY_NOT_PRESERVED` as the
reply, a line the app composes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence, TextIO

from henk.agent.session import HANDOFF_TOOL_NAME
from henk.agent.triage import check_triage_arc
from henk.replay.case import iso
from henk.replay.harness import SERVED_NOT_RECORDED
from henk.replay.recorder import is_recording_id
from henk.replay.run import (
    RUN_SCHEMA,
    ReplayRefused,
    ReplaySource,
    resolve_source,
    run_directory,
)

#: How much of a handoff document ``compare`` shows.
HANDOFF_START_CHARS = 240
#: How much of one arc line ``compare`` shows.
ARC_LINE_CHARS = 300

ORIGINAL = "original"

#: The reply shown for an original whose owner-facing reply was not preserved.
#: The app composes it from the recorded diagnosis and confidence; it is
#: bracketed and says who wrote it, so it cannot pass for the triage's own text.
REPLY_NOT_PRESERVED = (
    "[Composed by the replay tool, not written by the triage: this triage's "
    "owner-facing reply was not preserved. Its recorded diagnosis: {diagnosis}. "
    "Its recorded confidence: {confidence}.]"
)
NONE_RECORDED = "none recorded"

#: Where an original candidate came from, as the grade file records it.
FROM_RECORDING = "recording"
FROM_CASE_CANDIDATE = "case-original-candidate"

_ARC_LINE = re.compile(r"(?i)^\s*(?:diagnosis|(?:suggested\s+)?fix|pickup)\b\s*[:\-]")
#: C0 and C1 controls except tab; newlines are handled by the caller.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def terminal_safe(text: Any) -> str:
    """``text`` with every control character removed (a newline becomes a space)."""
    return _CONTROL.sub("", str(text).replace("\r", " ").replace("\n", " "))


# --- Loading runs -----------------------------------------------------------------


def _read_run(path, source: ReplaySource) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(record, dict) or record.get("schema") != RUN_SCHEMA:
        raise ValueError(f"not a {RUN_SCHEMA} run")
    if (record.get("source") or {}).get("id") != source.source_id:
        raise ReplayRefused(
            f"run {path.stem} is not a run of {source.source_id}; it names another "
            f"source ({(record.get('source') or {}).get('id')!r})"
        )
    return record


def load_runs(
    config: Any,
    source: ReplaySource,
    run_ids: Sequence[str],
    *,
    warn: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """The named runs of ``source``, in the order named.

    With no ids, every run file of the source, oldest id first: an unreadable
    one is skipped with a warning. A named run that is not an id, does not exist,
    cannot be read, or belongs to another source is refused.
    """
    directory = run_directory(config, source)
    if not run_ids:
        runs = []
        paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
        for path in paths:
            if not is_recording_id(path.stem):
                continue
            try:
                runs.append(_read_run(path, source))
            except (OSError, ValueError, ReplayRefused) as exc:
                if warn is not None:
                    warn(f"warning: skipping unreadable run {path.stem} "
                         f"({type(exc).__name__})")
        return runs
    runs = []
    for run_id in run_ids:
        if not is_recording_id(run_id):
            raise ReplayRefused(f"{run_id!r} is not a run id")
        path = directory / f"{run_id}.json"
        if not path.is_file():
            raise ReplayRefused(f"no run {run_id} of {source.source_id} in {directory}")
        try:
            runs.append(_read_run(path, source))
        except (OSError, ValueError) as exc:
            raise ReplayRefused(
                f"run {run_id} cannot be read ({type(exc).__name__})") from None
    return runs


# --- One candidate's view ---------------------------------------------------------


def original_handoffs(recording: Mapping[str, Any]) -> list[str]:
    """The handoff documents the original triage published, from its transcript.

    A reconstructed recording's calls carry ``arguments: "unknown"``, so this is
    empty for it: its handoff comes from the case's ``original_candidate``
    (:func:`original_of`).
    """
    documents = []
    for call in recording.get("transcript") or ():
        arguments = call.get("arguments")
        if call.get("name") == HANDOFF_TOOL_NAME and isinstance(arguments, Mapping):
            document = arguments.get("document")
            if isinstance(document, str):
                documents.append(document)
    return documents


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class Original:
    """The original triage as a candidate: what ``grade`` and ``compare`` show."""

    reply: Any  # the recording's reply, as recorded (null when not preserved)
    handoffs: tuple[str, ...]
    diagnosis: str | None  # the case's recorded diagnosis (original_candidate)
    confidence: str | None
    arc_complete: bool | None
    source: str  # FROM_RECORDING or FROM_CASE_CANDIDATE
    reconstructed: bool

    @property
    def reply_preserved(self) -> bool:
        return _text(self.reply) is not None

    @property
    def empty(self) -> bool:
        """No reply, no handoff and no recorded diagnosis: nothing to grade."""
        return not self.reply_preserved and not self.handoffs and self.diagnosis is None

    @property
    def reply_composed(self) -> bool:
        """The reply shown is :data:`REPLY_NOT_PRESERVED`, not the recorded one."""
        return not self.reply_preserved and self.source == FROM_CASE_CANDIDATE

    def shown_reply(self) -> Any:
        """The reply as the judge sees it: the recorded one, or, for a case
        whose reply was not preserved, :data:`REPLY_NOT_PRESERVED`."""
        if not self.reply_composed:
            return self.reply
        return REPLY_NOT_PRESERVED.format(diagnosis=self.diagnosis or NONE_RECORDED,
                                          confidence=self.confidence or NONE_RECORDED)


def original_of(source: ReplaySource) -> Original:
    """The source's original candidate.

    The recording's reply and transcript handoffs, plus, for a case carrying an
    ``original_candidate``, its handoff document, diagnosis and confidence. The
    candidate's model is never read here: it stays in the grade file's mapping.
    """
    recording = source.recording
    handoffs = original_handoffs(recording)
    candidate = source.case.original_candidate if source.case is not None else None
    if candidate is None:
        return Original(reply=recording.get("reply"), handoffs=tuple(handoffs),
                        diagnosis=None, confidence=None, arc_complete=None,
                        source=FROM_RECORDING, reconstructed=source.reconstructed)
    document = _text(candidate.get("handoff_document"))
    if document is not None and document not in handoffs:
        handoffs.append(document)
    arc = candidate.get("triage_arc_complete")
    return Original(
        reply=recording.get("reply"),
        handoffs=tuple(handoffs),
        diagnosis=_text(candidate.get("diagnosis")),
        confidence=_text(candidate.get("confidence")),
        arc_complete=arc if isinstance(arc, bool) else None,
        source=FROM_CASE_CANDIDATE,
        reconstructed=source.reconstructed,
    )


def _arc_lines(reply: Any) -> list[str]:
    return [line.strip()[:ARC_LINE_CHARS] for line in str(reply or "").splitlines()
            if _ARC_LINE.match(line)]


def _column(*, header: str, profile: Mapping[str, Any] | None, ending: Any, reply: Any,
            handoffs: Sequence[str], calls: Sequence[tuple[str, str | None]],
            usage: Mapping[str, Any] | None, unrecorded: int | None,
            notes: Sequence[str] = ()) -> dict[str, Any]:
    arc = check_triage_arc(str(reply or ""))
    profile = profile or {}
    return {
        "header": header,
        "model": profile.get("model"),
        "effort": profile.get("effort"),
        "thinking": profile.get("thinking"),
        "ending": ending,
        "arc_complete": arc.complete,
        "confidence": arc.confidence,
        "arc_lines": _arc_lines(reply),
        "handoff_start": handoffs[0][:HANDOFF_START_CHARS] if handoffs else None,
        "calls": list(calls),
        "usage": usage,
        "unrecorded": unrecorded,
        "notes": list(notes),
    }


def _original_column(source: ReplaySource) -> dict[str, Any]:
    recording = source.recording
    original = original_of(source)
    notes: list[str] = []
    if original.reconstructed and not original.reply_preserved:
        if original.source == FROM_CASE_CANDIDATE:
            notes.append("reply: not preserved; shown instead: the recorded diagnosis "
                         "and confidence and the original handoff")
        else:
            notes.append("reply: not preserved, and the case holds no original "
                         "candidate")
    if original.source == FROM_CASE_CANDIDATE:
        notes.append(f"recorded diagnosis: {original.diagnosis or NONE_RECORDED}")
    column = _column(
        header=f"{ORIGINAL} ({'reconstructed' if source.reconstructed else 'recorded'} "
               f"{iso(recording.get('at'))})",
        profile=recording.get("profile"),
        ending=(recording.get("ending") or {}).get("outcome"),
        reply=original.reply,
        handoffs=original.handoffs,
        calls=[(str(c.get("name")), None) for c in recording.get("transcript") or ()],
        usage=recording.get("usage"),
        unrecorded=None,
        notes=notes,
    )
    if original.source == FROM_CASE_CANDIDATE and not original.reply_preserved:
        # No reply to read an arc from: the audit record's own values stand.
        column["confidence"] = original.confidence
        column["arc_complete"] = original.arc_complete
    return column


def _run_column(run: Mapping[str, Any]) -> dict[str, Any]:
    return _column(
        header=f"run {run.get('run_id')} ({run.get('at_iso')})",
        profile=run.get("profile"),
        ending=(run.get("ending") or {}).get("outcome"),
        reply=run.get("reply"),
        handoffs=[h.get("document") for h in run.get("captured_handoffs") or ()
                  if isinstance(h.get("document"), str)],
        calls=[(str(c.get("name")), str(c.get("served"))) for c in run.get("tool_calls") or ()],
        usage=run.get("usage"),
        unrecorded=run.get("unrecorded_count"),
    )


# --- Rendering ------------------------------------------------------------------------


def _value(value: Any) -> str:
    return "-" if value is None else terminal_safe(value)


def _tokens(usage: Mapping[str, Any] | None) -> str:
    if not usage:
        return "-"
    return (f"in {_value(usage.get('input_tokens'))}, "
            f"out {_value(usage.get('output_tokens'))}, "
            f"cache read {_value(usage.get('cache_read_input_tokens'))}")


def _tag(served: str | None) -> str:
    if served is None:
        return ""
    if served == SERVED_NOT_RECORDED:
        return " [NOT RECORDED]"
    return f" [{terminal_safe(served)}]"


def _arc(value: bool | None) -> str:
    return "unknown" if value is None else ("complete" if value else "incomplete")


def render_compare(source: ReplaySource, runs: Sequence[Mapping[str, Any]]) -> str:
    """The side-by-side text: a summary row per candidate, then one section each."""
    columns = [_original_column(source)] + [_run_column(run) for run in runs]
    lines = [f"compare {source.kind} {terminal_safe(source.source_id)}"]
    if source.case is not None and source.case.statement():
        lines.append(terminal_safe(source.case.statement()))
    lines.append("")
    for column in columns:
        unrecorded = (f", {column['unrecorded']} unrecorded"
                      if column["unrecorded"] is not None else "")
        lines.append(
            f"{terminal_safe(column['header'])}  {_value(column['model'])} "
            f"{_value(column['effort'])}  {_value(column['ending'])}  "
            f"arc {_arc(column['arc_complete'])}  "
            f"confidence {_value(column['confidence'])}  "
            f"{len(column['calls'])} calls{unrecorded}"
        )
    if not runs:
        lines.append("(no replay runs of this source yet)")
    for column in columns:
        lines += ["", f"== {terminal_safe(column['header'])}",
                  f"model: {_value(column['model'])}; effort: {_value(column['effort'])}; "
                  f"thinking: {_value(column['thinking'] or 'unset')}",
                  f"ending: {_value(column['ending'])}; arc: "
                  f"{_arc(column['arc_complete'])}; "
                  f"confidence: {_value(column['confidence'] or 'none')}"]
        lines += [terminal_safe(note) for note in column["notes"]]
        lines += [f"  | {terminal_safe(line)}" for line in column["arc_lines"]] or [
            "  | (no arc lines)"]
        start = column["handoff_start"]
        lines.append(f"handoff: {terminal_safe(start)}" if start is not None
                     else "handoff: none captured")
        lines.append("tool calls:" if column["calls"] else "tool calls: none")
        for n, (name, served) in enumerate(column["calls"], start=1):
            lines.append(f"  {n}. {terminal_safe(name)}{_tag(served)}")
        lines.append(f"tokens: {_tokens(column['usage'])}")
    return "\n".join(lines) + "\n"


# --- The command ----------------------------------------------------------------------


def command(config: Any, args: Any, *, stdout: TextIO, stderr: TextIO, **_: Any) -> int:
    """``compare <id> [run ...]``: read, render, print. Nothing else."""
    source = resolve_source(config, args.id)
    runs = load_runs(config, source, args.runs, warn=lambda line: print(line, file=stderr))
    stdout.write(render_compare(source, runs))
    return 0
