"""Running one replay, and writing its run file (triage-quality D14).

A replay re-sends a recording's composed content, byte for byte, to a session
built by the production :class:`~henk.agent.sdk_session.SdkSessionFactory`
over the replay registry (:mod:`henk.replay.harness`) and a fresh
:class:`~henk.gate.approval.ApprovalGate`:

- the same ``PreToolUse`` hook, the same empty ``allowed_tools``, the same
  ``can_use_tool`` -> gate path, because it is the same factory class;
- the system prompt composed from the current configuration;
- the owner's model, effort and thinking, validated here before anything that
  could spend (:func:`validate_model`, :func:`validate_effort`,
  :func:`validate_thinking`);
- the gate framed as a tainted, non-announceable event turn, so every mutating
  call is denied out of scope, with its receipt written to the run file;
- nothing added to the content: no recall block, no digest, no time header. The
  replay reads no memory and no handoff archive, so neither can reach the model
  except as the recording itself holds them.

**Drift is reported, not hidden.** The run compares the recording's system-prompt
and tool-definition hashes with the current ones (``same``, ``differs``, or
``unknown`` when the recording holds none), and for a reconstructed case lists
every captured answer the current registry would not be served
(:func:`henk.replay.case.capture_drift`), plus those met while serving.

**The run file** goes to ``<triage_replays_dir>/<source id>/<run_id>.json``, a
path derived from ``audit.path``, written atomically (a temp file in the same
directory, then a rename), mode 600 in a mode-700 directory. It holds the reply,
the tool calls (each tagged with how it was answered), captured handoffs and
notifications, authorization decisions, the unrecorded count, usage, the profile
and the drift. Group 12a's ``compare`` and ``grade`` read it
(:data:`RUN_SCHEMA`).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from henk.agent.ending import COMPLETED, classify_ending
from henk.agent.permission import base_tool_name
from henk.agent.sdk_session import SdkSessionFactory
from henk.agent.session import EVENT_PROFILE, TurnEnding
from henk.agent.triage import check_triage_arc, extract_diagnosis
from henk.config import EFFORT_LEVELS, THINKING_MODES
from henk.gate.approval import ApprovalGate, TurnContext
from henk.replay.case import (
    CaptureIndex,
    CaseInvalid,
    ReconstructedQueries,
    ReplayCase,
    capture_drift,
    iso,
    load_case,
)
from henk.replay.harness import (
    NullChannel,
    RefusingTransport,
    ReplayReceipts,
    ReplayServer,
    build_definition_registry,
    build_replay_registry,
    canonical_arguments,
)
from henk.replay.recorder import (
    CASE_FILE,
    _CASE_ID_RE,
    is_recording_id,
    list_cases,
    load_recording,
    new_recording_id,
    recording_path,
    system_prompt_hash,
    tool_definitions_hash,
    write_atomically,
)
from henk.tools.base import TurnType

RUN_SCHEMA = "henk.triage-replay-run.v1"

#: A Claude model identifier (design D14). Matched whole, so a trailing newline
#: or space is refused rather than slipping past a ``$``.
MODEL_PATTERN = re.compile(r"claude-[a-z0-9-]+(\[1m\])?")
MODEL_FORM = (
    "claude-<name> in lowercase letters, digits and hyphens, optionally followed by "
    "[1m] (for example claude-opus-5-5 or claude-opus-5-5[1m])"
)

#: The replay's gate framing (D14): an event turn, never announceable, and the
#: session tainted, so no write is ever in scope and no prompt is ever sent.
REPLAY_TURN = TurnContext(turn_type=TurnType.EVENT, announceable=False, tainted=True)


class ReplayRefused(RuntimeError):
    """The replay will not run; nothing was sent and nothing spent."""


# --- Validation before any spend ---------------------------------------------


def validate_model(model: Any) -> str:
    if not isinstance(model, str) or MODEL_PATTERN.fullmatch(model) is None:
        raise ReplayRefused(
            f"--model {model!r} is not a Claude model identifier; the expected form "
            f"is {MODEL_FORM}. No model was called."
        )
    return model


def validate_effort(effort: Any) -> str:
    if effort not in EFFORT_LEVELS:
        raise ReplayRefused(
            f"--effort {effort!r} is not an SDK effort level; accepted values: "
            f"{', '.join(EFFORT_LEVELS)}. No model was called."
        )
    return effort


def validate_thinking(thinking: Any) -> str | None:
    if thinking is not None and thinking not in THINKING_MODES:
        raise ReplayRefused(
            f"--thinking {thinking!r} is not a thinking mode; accepted values: "
            f"{', '.join(THINKING_MODES)}, or omit it to leave thinking unset. No "
            "model was called."
        )
    return thinking


def check_audit_log(config: Any) -> Path:
    """Refuse to run where the audit log does not exist (D14).

    From a copy of the checkout, such as ``henk.old``, compose resolves the
    project to ``henkold`` and mounts brand-new, empty volumes: no audit log, no
    recordings, and a run file that would land where no one looks.
    """
    path = Path(config.audit.path)
    if not path.is_file():
        raise ReplayRefused(
            f"the audit log {path} does not exist. The compose project may have "
            "resolved to a copy of the checkout with empty volumes (a directory "
            "named henk.old becomes project henkold). Run from the henk checkout "
            "directory, where the project is henk. Nothing was run and no model was "
            "called."
        )
    return path


# --- What is replayed -----------------------------------------------------------


@dataclass(frozen=True)
class ReplaySource:
    kind: str  # "recording" | "case"
    source_id: str
    recording: dict[str, Any]
    case: ReplayCase | None = None

    @property
    def reconstructed(self) -> bool:
        return bool(self.recording.get("reconstructed"))


def resolve_source(config: Any, ident: str) -> ReplaySource:
    """A recording id names a recording; any other id must be a reference case."""
    if is_recording_id(ident):
        path = recording_path(config.audit.triage_recordings_dir, ident)
        if not path.is_file():
            raise ReplayRefused(f"no recording {ident} in {path.parent}")
        try:
            recording = load_recording(config.audit.triage_recordings_dir, ident)
        except (OSError, ValueError) as exc:
            raise ReplayRefused(
                f"recording {ident} cannot be read ({type(exc).__name__})") from None
        if recording.get("reconstructed"):
            raise ReplayRefused(
                f"recording {ident} is reconstructed; a reconstructed recording is "
                "replayed only as a reference case, with its capture"
            )
        return ReplaySource("recording", ident, recording)
    cases_dir = config.audit.triage_cases_dir
    if ident not in list_cases(cases_dir):
        raise ReplayRefused(
            f"no recording or reference case named {ident!r}. A case is a directory "
            f"in {cases_dir} holding {CASE_FILE}; raw and capture material is not one."
        )
    try:
        case = load_case(cases_dir, ident)
    except CaseInvalid as exc:
        raise ReplayRefused(str(exc)) from None
    return ReplaySource("case", ident, case.recording, case)


# --- The run ----------------------------------------------------------------------


def _default_create_session(factory: SdkSessionFactory):  # pragma: no cover - SDK
    return factory.create()


@dataclass
class ReplayOutcome:
    record: dict[str, Any]
    factory: SdkSessionFactory
    transport: RefusingTransport
    channel: NullChannel
    server: ReplayServer


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _hash_drift(recorded: Any, current: str) -> dict[str, Any]:
    if not isinstance(recorded, str):
        status = "unknown"
    else:
        status = "same" if recorded == current else "differs"
    return {"recorded": recorded if isinstance(recorded, str) else None,
            "current": current, "status": status}


def _getter(session: Any, name: str) -> Any:
    getter = getattr(session, name, None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001 - optional protocol methods, like the core's
        return None


def _tag_calls(transcript: Any, server: ReplayServer, registry_names) -> list[dict]:
    """The session's calls, each tagged with how the replay answered it."""
    served: dict[tuple[str, str], deque] = defaultdict(deque)
    for entry in server.served:
        served[(entry.name, canonical_arguments(entry.arguments))].append(entry)
    names = set(registry_names)
    calls = []
    for call in transcript or ():
        name = base_tool_name(str(getattr(call, "name", "")))
        arguments = getattr(call, "arguments", None)
        arguments = dict(arguments) if isinstance(arguments, Mapping) else {}
        queue = served.get((name, canonical_arguments(arguments)))
        if queue:
            tag = queue.popleft().served
        elif name in names:
            tag = "denied"
        else:
            tag = "blocked"
        calls.append({
            "name": name,
            "arguments": arguments,
            "result": getattr(call, "result", None),
            "is_error": getattr(call, "is_error", None),
            "served": tag,
        })
    return calls


async def run_replay(
    config: Any,
    source: ReplaySource,
    *,
    model: str,
    effort: str,
    thinking: str | None = None,
    create_session: Callable[[SdkSessionFactory], Any] = _default_create_session,
    clock: Callable[[], float] = time.time,
) -> ReplayOutcome:
    """Replay ``source`` once on ``model``/``effort``/``thinking``.

    Validates again before anything else, so no caller can reach a session with
    an unchecked model or effort. The model call is the only request this makes.
    """
    validate_model(model)
    validate_effort(effort)
    validate_thinking(thinking)
    recording = source.recording
    content = recording.get("content")
    if not isinstance(content, str):
        raise ReplayRefused("the recording holds no composed content to re-send")

    max_points = config.homelab_query.query_range_max_points
    queries = None
    listed_capture_drift: list[dict[str, Any]] = []
    case = source.case
    if source.reconstructed:
        if case is None or case.capture_dir is None or case.t is None:
            raise ReplayRefused("a reconstructed recording is replayed only with its capture")
        index = CaptureIndex.load(case.capture_dir)
        queries = ReconstructedQueries(index, t=case.t, max_points=max_points)
        listed_capture_drift = capture_drift(index, t=case.t, max_points=max_points)

    transport = RefusingTransport()
    channel = NullChannel()
    receipts = ReplayReceipts()
    server = ReplayServer(recording, queries=queries)
    client = httpx.AsyncClient(transport=transport)
    try:
        definitions = build_definition_registry(config, client)
        registry = build_replay_registry(definitions, server)
        gate = ApprovalGate(
            channel,
            timeout_seconds=config.agent.approval_timeout_seconds,
            demote_standing=config.gate.demote_standing,
            recorder=receipts,
        )
        factory = SdkSessionFactory(
            registry,
            gate,
            model=model,
            system_prompt=config.agent.system_prompt,
            effort=effort,
            thinking=thinking,
            profile=EVENT_PROFILE,
        )
        hashes = recording.get("hashes") or {}
        drift = {
            "system_prompt": _hash_drift(
                hashes.get("system_prompt"), system_prompt_hash(config.agent.system_prompt)
            ),
            "tool_definitions": _hash_drift(
                hashes.get("tool_definitions"), tool_definitions_hash(registry.tools())
            ),
        }

        session = create_session(factory)
        reply: str | None = None
        raised = False
        gate.enter_turn(REPLAY_TURN)
        try:
            reply = await session.run_turn(content)
        except Exception:  # noqa: BLE001 - recorded as the run's ending
            raised = True
        finally:
            gate.exit_turn()
        signals = _getter(session, "ending")
        transcript = _getter(session, "transcript")
        stats = _getter(session, "stats")
        try:
            await session.close()
        except Exception:  # noqa: BLE001 - the run is written regardless
            pass
    finally:
        await client.aclose()

    ending = classify_ending(
        signals if isinstance(signals, TurnEnding) else None, raised=raised, reply=reply
    )
    arc = None
    if ending.outcome == COMPLETED:
        checked = check_triage_arc(reply or "")
        arc = {"complete": checked.complete, "diagnosis": checked.diagnosis,
               "fix": checked.fix, "pickup": checked.pickup,
               "confidence": checked.confidence,
               "diagnosis_text": extract_diagnosis(reply or "")}
    served_drift = list(queries.drift) if queries is not None else []
    capture_listed = listed_capture_drift
    drift["capture"] = capture_listed
    drift["capture_served"] = served_drift
    drift["any"] = (
        any(drift[k]["status"] == "differs" for k in ("system_prompt", "tool_definitions"))
        or bool(capture_listed) or bool(served_drift)
    )
    at = float(clock())
    record = {
        "schema": RUN_SCHEMA,
        "run_id": new_recording_id(at),
        "at": at,
        "at_iso": iso(at),
        "source": {"kind": source.kind, "id": source.source_id,
                   "recording_id": recording.get("recording_id")},
        "reconstructed": source.reconstructed,
        "case": case.describe() if case is not None else None,
        "profile": {"name": EVENT_PROFILE, "model": model, "effort": effort,
                    "thinking": thinking},
        "recorded_profile": recording.get("profile"),
        "content_sha256": _sha256(content),
        "drift": drift,
        "reply": reply,
        "raised": raised,
        "ending": {"outcome": ending.outcome, "error_class": ending.error_class,
                   "http_status": ending.http_status},
        "arc": arc,
        "tool_calls": _tag_calls(transcript, server, registry.names()),
        "unrecorded_count": len(server.unrecorded),
        "unrecorded_calls": [{"name": c.name, "arguments": c.arguments}
                             for c in server.unrecorded],
        "unavailable_count": len(server.unavailable),
        "captured_handoffs": list(server.handoffs),
        "captured_notifications": list(server.notifications),
        "authorization_decisions": list(receipts.decisions),
        "isolation": {"refused_tool_requests": len(transport.attempts),
                      "channel_attempts": len(channel.attempts)},
        "usage": (
            {"input_tokens": getattr(stats, "input_tokens", None),
             "output_tokens": getattr(stats, "output_tokens", None),
             "cache_read_input_tokens": getattr(stats, "cache_read_input_tokens", None)}
            if stats is not None else None
        ),
        "observed_model": getattr(stats, "model", None) if stats is not None else None,
        "original_calls": [
            {"name": c.get("name"), "arguments": c.get("arguments")}
            for c in recording.get("transcript") or ()
        ],
    }
    return ReplayOutcome(record=record, factory=factory, transport=transport,
                         channel=channel, server=server)


# --- The run writer ---------------------------------------------------------------


def run_directory(config: Any, source: ReplaySource) -> Path:
    """``<triage_replays_dir>/<source id>/``. Retention removes a recording's
    directory with it; a case's is kept with the case."""
    if not (is_recording_id(source.source_id) or _CASE_ID_RE.match(source.source_id)) \
            or ".." in source.source_id:
        raise ValueError(f"not a replay source id: {source.source_id!r}")
    return Path(config.audit.triage_replays_dir) / source.source_id


def write_run(config: Any, source: ReplaySource, record: Mapping[str, Any]) -> Path:
    """Write one run file atomically; its name is the run id, which is checked."""
    run_id = record.get("run_id")
    if not is_recording_id(run_id):
        raise ValueError(f"not a run id: {run_id!r}")
    directory = run_directory(config, source)
    for level in (directory.parent, directory):
        level.mkdir(mode=0o700, exist_ok=True)
        level.chmod(0o700)
    data = (json.dumps(dict(record), ensure_ascii=True, sort_keys=True, indent=1)
            + "\n").encode("ascii")
    path = directory / f"{run_id}.json"
    write_atomically(path, data)
    return path
