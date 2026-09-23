"""Triage recording: one bounded local recording per event triage (design D13).

A recording holds exactly what the model was sent and what it did: the composed
content byte-exact, the profile, the system-prompt and tool-definition hashes,
every tool call of the triage turn with its arguments and result text (denied
calls included), the reply and the ending. Group 11 replays it against another
model; group 12 grades it.

**Not an audit record.** A recording has its own file family and schema
(`schema/triage-recording.v1.schema.json`), lives in `triage-recordings/` beside
the audit log (`AuditConfig.triage_recordings_dir`, derived, never configured),
may be deleted by retention, and is never read by cadence rehydration. It holds
tool output and memory content, which the audit path deliberately does not
(`RESULT_CAPTURING_TOOLS`), and tailnet addresses, so it never enters the
repository. The audit record links it by id only.

**Never disturbs the triage.** :meth:`TriageRecorder.record` never raises: a
failure is logged at error level, with no content, and returns None, so the
triage's record carries a null link, never a false one.

**Bounds** are module constants, reviewed in code (D17): 256 KB per recording,
200 recordings, 30 days. An oversized recording has its largest tool results
shortened, each with an explicit marker, and is flagged incomplete. Retention runs
after every write, oldest first, and removes each pruned recording's replay output
in `triage-replays/<recording_id>/` with it. It never touches `triage-cases/`:
reference cases are kept deliberately, bounded at 20 by count, and adding one past
that is refused rather than evicting one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from henk.agent.triage import incident_times

logger = logging.getLogger("henk.replay.recorder")

RECORDING_SCHEMA = "henk.triage-recording.v1"
RECORDING_SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schema" / "triage-recording.v1.schema.json"
)

#: The per-recording size bound, in bytes of the file as written.
MAX_RECORDING_BYTES = 256 * 1024
#: How many recordings are kept (evidence-probe 1.4: the worst 30 days held 16).
MAX_RECORDINGS = 200
#: How old a recording may get. A recording exactly this old is kept.
MAX_RECORDING_AGE_SECONDS = 30 * 24 * 3600
#: How many reference cases `triage-cases/` may hold. Past it, adding is refused.
MAX_REFERENCE_CASES = 20
#: A reference case is a directory holding this file, and nothing else counts.
CASE_FILE = "case.json"

#: A leftover temp file (a crash between write and rename) older than this is
#: removed by retention; a younger one may be a write in progress.
_STALE_TEMP_SECONDS = 3600

TRUNCATION_MARKER = "[truncated by the recorder: {n} bytes removed]"
INCOMPLETE_TRUNCATED = "truncated"
INCOMPLETE_NO_TRANSCRIPT = "transcript-unavailable"

_ID_RE = re.compile(r"^([0-9]{8}T[0-9]{6}Z)-([0-9a-f]{8})$")
_ID_TIME_FORMAT = "%Y%m%dT%H%M%SZ"
_SUFFIX = ".json"
_TEMP_PREFIX = "."
_TEMP_SUFFIX = ".tmp"
#: A case id: one path component, no leading dot, no separators.
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RecordingTooLarge(ValueError):
    """The recording cannot fit its bound without cutting more than tool results."""


class CaseRefused(RuntimeError):
    """A reference case was not added; nothing was written."""


class CaseBoundReached(CaseRefused):
    """Adding a case would pass :data:`MAX_REFERENCE_CASES`; none is evicted."""


# --- Ids --------------------------------------------------------------------


def new_recording_id(at: float) -> str:
    """``<UTC stamp>-<8 hex>``: sorts chronologically, and names its file."""
    stamp = datetime.fromtimestamp(at, timezone.utc).strftime(_ID_TIME_FORMAT)
    return f"{stamp}-{secrets.token_hex(4)}"


def is_recording_id(value: Any) -> bool:
    return isinstance(value, str) and _ID_RE.match(value) is not None


def recording_time(recording_id: str) -> float | None:
    """The epoch second a recording id names, or None when it is not an id."""
    match = _ID_RE.match(recording_id) if isinstance(recording_id, str) else None
    if match is None:
        return None
    try:
        parsed = datetime.strptime(match.group(1), _ID_TIME_FORMAT)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


# --- Fingerprints (group 11 compares its own against these) -----------------


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def system_prompt_hash(system_prompt: str) -> str:
    return _sha256(system_prompt)


def tool_definitions_hash(tools: Iterable[Any]) -> str:
    """A hash of what the model is shown of each tool: name, description, schema.

    Order-independent (sorted by name), and over the same three fields the SDK
    adapter hands the in-process MCP server (``sdk_session._adapt_tool``), so a
    replay registry of stubs with the current definitions hashes identically.
    """
    definitions = sorted(
        (
            {
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", ""),
                "parameters": getattr(tool, "parameters", {}),
            }
            for tool in tools
        ),
        key=lambda d: d["name"],
    )
    return _sha256(
        json.dumps(definitions, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )


def factory_fingerprint(factory: Any) -> dict[str, Any]:
    """The profile, model, effort, thinking and hashes a session factory runs with.

    Read with ``getattr``: a factory that exposes none of them (a test fake) is
    ``chat`` with every other field unknown (None), never an invented value.
    """
    config = getattr(factory, "config", None)
    registry = getattr(factory, "registry", None)
    prompt = getattr(config, "system_prompt", None)
    tools = getattr(registry, "tools", None)
    return {
        "profile": getattr(factory, "profile", "chat"),
        "model": getattr(config, "model", None) if config is not None
        else getattr(factory, "model", None),
        "effort": getattr(config, "effort", None) if config is not None
        else getattr(factory, "effort", None),
        "thinking": getattr(config, "thinking", None),
        "system_prompt_sha256": (
            system_prompt_hash(prompt) if isinstance(prompt, str) else None
        ),
        "tool_definitions_sha256": (
            tool_definitions_hash(tools()) if callable(tools) else None
        ),
    }


# --- Building ---------------------------------------------------------------


def _incident(item: Any) -> dict[str, Any]:
    ident, event = item.identity, item.event
    times = incident_times(event)
    return {
        "identity_key": ident.key,
        "source": ident.source,
        "name": ident.name,
        "state": ident.state.value,
        "event_id": event.id,
        "title": event.title,
        "message": event.message,
        "notified": times.notified,
        "received": times.received,
        "recurrence": bool(item.recurrence),
        "prior_handoff_ref": item.prior_handoff_ref,
    }


def _call(call: Any) -> dict[str, Any]:
    arguments = getattr(call, "arguments", None)
    result = getattr(call, "result", None)
    is_error = getattr(call, "is_error", None)
    return {
        "tool_use_id": getattr(call, "tool_use_id", None),
        "name": str(call.name),
        "arguments": dict(arguments) if isinstance(arguments, Mapping) else {},
        "result": result if isinstance(result, str) else None,
        "is_error": is_error if isinstance(is_error, bool) else None,
    }


def _usage(stats: Any) -> dict[str, Any] | None:
    if stats is None:
        return None
    return {
        "input_tokens": getattr(stats, "input_tokens", None),
        "output_tokens": getattr(stats, "output_tokens", None),
        "cache_read_input_tokens": getattr(stats, "cache_read_input_tokens", None),
    }


def build_recording(
    *,
    recording_id: str,
    at: float,
    turn: Any,
    content: str,
    reply: str | None,
    ending: Any,
    fingerprint: Mapping[str, Any],
    transcript: Sequence[Any] | None,
    stats: Any = None,
    prior_handoff_ids: Iterable[int] = (),
    approvals: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """One live recording, before the size bound. ``transcript=None`` means the
    session exposed none: recorded as incomplete, never as a turn with no calls."""
    reasons = [] if transcript is not None else [INCOMPLETE_NO_TRANSCRIPT]
    return {
        "schema": RECORDING_SCHEMA,
        "recording_id": recording_id,
        "at": float(at),
        "reconstructed": False,
        "incidents": [_incident(item) for item in turn.items],
        "announceable": bool(turn.announceable),
        "suppressed_count": int(turn.suppressed_count),
        "content": content,
        "profile": {
            "name": fingerprint.get("profile", "chat"),
            "model": fingerprint.get("model"),
            "effort": fingerprint.get("effort"),
            "thinking": fingerprint.get("thinking"),
        },
        "hashes": {
            "system_prompt": fingerprint.get("system_prompt_sha256"),
            "tool_definitions": fingerprint.get("tool_definitions_sha256"),
        },
        "transcript": [_call(c) for c in (transcript or ())],
        "reply": reply,
        "ending": {
            "outcome": ending.outcome,
            "error_class": ending.error_class,
            "http_status": ending.http_status,
        },
        "complete": not reasons,
        "incomplete_reasons": reasons,
        "prior_handoff_ids": [int(i) for i in prior_handoff_ids],
        "approvals": [
            {
                "tool": a.get("tool"),
                "tier": a.get("tier"),
                "outcome": a.get("outcome"),
                "initiated_by": a.get("initiated_by", "model"),
                "reference": a.get("reference"),
            }
            for a in approvals
        ],
        "usage": _usage(stats),
        "observed_model": getattr(stats, "model", None) if stats is not None else None,
    }


# --- The size bound -----------------------------------------------------------


def serialize(record: Mapping[str, Any]) -> bytes:
    """The bytes written. ASCII-escaped, so no text (a lone surrogate included)
    can fail the encode, and the bound counts exactly what lands on disk."""
    return (json.dumps(record, ensure_ascii=True, sort_keys=True, indent=1) + "\n").encode(
        "ascii"
    )


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def _json_len(text: str) -> int:
    """What ``text`` costs in the file: its escaped JSON form, quotes included."""
    return len(json.dumps(text, ensure_ascii=True))


def _prefix_within(text: str, budget: int) -> str:
    """The longest prefix of ``text`` whose JSON form fits ``budget`` bytes. Cut by
    code point, so no character (a non-BMP one included) is ever split."""
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _json_len(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low]


def _cap_for(sizes: Sequence[int], removal: int) -> int:
    """The largest per-result cap that removes at least ``removal`` bytes: the
    largest results are shortened first, down to a common size."""
    low, high = 0, max(sizes, default=0)
    while low < high:
        mid = (low + high + 1) // 2
        if sum(max(0, n - mid) for n in sizes) >= removal:
            low = mid
        else:
            high = mid - 1
    return low


def apply_size_bound(
    record: dict[str, Any], *, limit: int = MAX_RECORDING_BYTES
) -> tuple[dict[str, Any], bytes]:
    """Return the record, and its bytes, within ``limit``.

    Only tool results are ever shortened: the composed content, the reply and the
    arguments are what a replay re-sends and matches on. The largest results are
    cut first, down to a common size in the file. Each shortened result ends with
    :data:`TRUNCATION_MARKER` naming the UTF-8 bytes removed from it, carries
    ``truncated_bytes``, and the recording is flagged incomplete. Raises
    :class:`RecordingTooLarge` when even empty results would not fit.
    """
    data = serialize(record)
    if len(data) <= limit:
        return record, data
    calls = record["transcript"]
    texts = [c["result"] if c["result"] is not None else "" for c in calls]
    sizes = [_json_len(t) - 2 if t else 0 for t in texts]
    # Room for each marker and `truncated_bytes` field, then whatever is left over.
    removal = len(data) - limit + 128 * sum(1 for n in sizes if n)
    for _ in range(12):
        cap = _cap_for(sizes, removal)
        shortened = []
        for call, text, size in zip(calls, texts, sizes):
            call = dict(call)
            if call["result"] is not None and size > cap:
                kept = _prefix_within(text, cap + 2)
                removed = _utf8_len(text) - _utf8_len(kept)
                call["result"] = f"{kept}\n{TRUNCATION_MARKER.format(n=removed)}"
                call["truncated_bytes"] = removed
            shortened.append(call)
        candidate = dict(record, transcript=shortened)
        if any("truncated_bytes" in c for c in shortened):
            reasons = [r for r in record["incomplete_reasons"] if r != INCOMPLETE_TRUNCATED]
            candidate["incomplete_reasons"] = [INCOMPLETE_TRUNCATED, *reasons]
            candidate["complete"] = False
        data = serialize(candidate)
        if len(data) <= limit:
            return candidate, data
        if cap == 0:
            break
        removal += len(data) - limit + 1024
    raise RecordingTooLarge(
        f"the recording exceeds {limit} bytes even with every tool result cut"
    )


# --- Writing ------------------------------------------------------------------


def _private_dir(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)


def write_atomically(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temp file in the same directory and a
    rename, so a reader (or a crash) never sees a partial file. Mode 600."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class TriageRecorder:
    """Writes one recording per event triage into ``directory``, then prunes.

    Constructed by the runtime only when ``triage_recording.enabled`` (default
    true); the core records nothing without one. Its constructor does no I/O: the
    directory is created, mode 700, on the first write.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        replays_dir: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._directory = Path(directory)
        self._replays_dir = Path(replays_dir) if replays_dir is not None else None
        self._clock = clock

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def replays_dir(self) -> Path | None:
        return self._replays_dir

    def record(
        self,
        *,
        turn: Any,
        content: str,
        reply: str | None,
        ending: Any,
        factory: Any = None,
        transcript: Sequence[Any] | None = None,
        stats: Any = None,
        prior_handoff_ids: Iterable[int] = (),
        approvals: Iterable[Mapping[str, Any]] = (),
    ) -> str | None:
        """Write the triage's recording. Returns its id, or None when none was
        written. Never raises, and logs no content: the exception's class only."""
        try:
            at = float(self._clock())
            recording_id = new_recording_id(at)
            record = build_recording(
                recording_id=recording_id,
                at=at,
                turn=turn,
                content=content,
                reply=reply,
                ending=ending,
                fingerprint=factory_fingerprint(factory),
                transcript=transcript,
                stats=stats,
                prior_handoff_ids=prior_handoff_ids,
                approvals=approvals,
            )
            _, data = apply_size_bound(record)
            _private_dir(self._directory)
            write_atomically(self._directory / f"{recording_id}{_SUFFIX}", data)
        except Exception as exc:  # noqa: BLE001 - recording must never fail a triage
            logger.error(
                "triage recording was not written (%s) in %s; the triage record "
                "links none",
                type(exc).__name__,
                self._directory,
            )
            return None
        try:
            self.prune(now=at)
        except Exception as exc:  # noqa: BLE001 - the recording exists; keep its id
            logger.error("triage recording retention failed (%s)", type(exc).__name__)
        return recording_id

    def prune(self, *, now: float | None = None) -> list[str]:
        """Apply the age and count bounds, oldest first. Returns the removed ids.

        Reads names only, and deletes only files it names itself
        (``<recording_id>.json``), their ``triage-replays/<recording_id>/``, and its
        own stale temp files. It never lists or touches ``triage-cases/``.
        """
        now = float(self._clock() if now is None else now)
        ids = list_recordings(self._directory)
        cutoff = now - MAX_RECORDING_AGE_SECONDS
        expired = [rid for rid in ids if _older_than(rid, cutoff)]
        kept = [rid for rid in ids if rid not in expired]
        overflow = kept[: max(0, len(kept) - MAX_RECORDINGS)]
        removed = []
        for rid in expired + overflow:
            try:
                (self._directory / f"{rid}{_SUFFIX}").unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.error("could not remove recording %s (%s)", rid, type(exc).__name__)
                continue
            self._remove_replays(rid)
            removed.append(rid)
        self._remove_stale_temps(now)
        return removed

    def _remove_replays(self, recording_id: str) -> None:
        if self._replays_dir is None or not is_recording_id(recording_id):
            return
        target = self._replays_dir / recording_id
        try:
            if target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        except OSError as exc:
            logger.error("could not remove replay output for %s (%s)", recording_id,
                         type(exc).__name__)

    def _remove_stale_temps(self, now: float) -> None:
        try:
            entries = list(os.scandir(self._directory))
        except OSError:
            return
        for entry in entries:
            if not (entry.name.startswith(_TEMP_PREFIX) and entry.name.endswith(_TEMP_SUFFIX)):
                continue
            try:
                if entry.is_file(follow_symlinks=False) and (
                    now - entry.stat(follow_symlinks=False).st_mtime > _STALE_TEMP_SECONDS
                ):
                    os.unlink(entry.path)
            except OSError:
                continue


def _older_than(recording_id: str, cutoff: float) -> bool:
    at = recording_time(recording_id)
    return at is not None and at < cutoff


# --- Reading (the group 11 seam) -----------------------------------------------


def list_recordings(directory: str | Path) -> list[str]:
    """The recording ids in ``directory``, oldest first. Names only, nothing read."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    ids = [
        name[: -len(_SUFFIX)]
        for name in names
        if name.endswith(_SUFFIX) and is_recording_id(name[: -len(_SUFFIX)])
    ]
    return sorted(ids)


def recording_path(directory: str | Path, recording_id: str) -> Path:
    """The file for ``recording_id``; a value that is not an id is refused, so no
    argument can name a path outside the directory."""
    if not is_recording_id(recording_id):
        raise ValueError(f"not a recording id: {recording_id!r}")
    return Path(directory) / f"{recording_id}{_SUFFIX}"


def load_recording(directory: str | Path, recording_id: str) -> dict[str, Any]:
    return json.loads(recording_path(directory, recording_id).read_text(encoding="ascii"))


# --- Reference cases (triage-cases/) -------------------------------------------


def list_cases(cases_dir: str | Path) -> list[str]:
    """The reference case ids: subdirectories holding a readable ``case.json``.

    Raw or capture material (a directory with no ``case.json``) is skipped
    quietly; an entry that cannot be read, or whose ``case.json`` is not a JSON
    object, is skipped with a warning naming the entry and nothing of its content.
    """
    root = Path(cases_dir)
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return []
    cases = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            case_file = Path(entry.path) / CASE_FILE
            if not case_file.exists():
                continue
            parsed = json.loads(case_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable reference case entry %s (%s)",
                           entry.name, type(exc).__name__)
            continue
        if not isinstance(parsed, dict):
            logger.warning("skipping reference case entry %s (case.json is not an object)",
                           entry.name)
            continue
        cases.append(entry.name)
    return cases


def add_case(
    cases_dir: str | Path,
    case_id: str,
    case: Mapping[str, Any],
    *,
    replace: bool = False,
) -> Path:
    """Write ``triage-cases/<case_id>/case.json`` atomically, within the bound.

    Refused, with nothing written: an id that is not one plain path component; an
    existing case unless ``replace`` (replacing is not an addition); a directory
    that exists without a ``case.json`` (raw or capture material is never turned
    into a case); and a new case when :data:`MAX_REFERENCE_CASES` already exist.
    No case is ever evicted to make room.
    """
    if not isinstance(case_id, str) or not _CASE_ID_RE.match(case_id) or ".." in case_id:
        raise CaseRefused(f"not a valid case id: {case_id!r}")
    root = Path(cases_dir)
    target = root / case_id
    existing = list_cases(root)
    if case_id in existing:
        if not replace:
            raise CaseRefused(f"reference case {case_id!r} already exists")
    else:
        if os.path.lexists(target):
            raise CaseRefused(
                f"{case_id!r} exists and is not a reference case; it is left untouched"
            )
        if len(existing) >= MAX_REFERENCE_CASES:
            raise CaseBoundReached(
                f"the reference case bound of {MAX_REFERENCE_CASES} is reached; "
                f"{case_id!r} was not added and no case is evicted"
            )
    _private_dir(target)
    data = (json.dumps(dict(case), ensure_ascii=True, sort_keys=True, indent=1) + "\n").encode(
        "ascii"
    )
    path = target / CASE_FILE
    write_atomically(path, data)
    return path
