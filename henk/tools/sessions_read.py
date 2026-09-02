"""sessions_read — the owner's workstation sessions, as last reported.

The workstation publisher posts one full-state snapshot to a dedicated ntfy
topic (design D6/D7); this tool reads that topic's cache at call time and
renders it. Four properties are the whole design:

- **Selection is a gate, and its non-pass outcomes are terminal** (D10). An empty
  allowlist, a failed request, an empty poll, a poll with no usable snapshot, and
  an unrecognised `schema` each end the result with one sentence and nothing
  after it. Nothing is ever fabricated to fill the gap.
- **Nothing rendered is free text.** Every value that reaches the owner is either
  one of this module's own sentences or a snapshot value that passed a shape
  check (`^[\\w:.-]{1,32}$` for `pane` and `project`, a closed status set, a
  non-negative integer age). A session that fails is *unusable* and is counted,
  never rendered — which is why an attacker-authored `project` cannot become a
  sentence in a turn where Henk's write verbs are available (D1).
- **Henk gates again.** The topic already carries the publisher's post-filter
  output, and Henk still applies `personal_data.session_project_allowlist`
  before rendering, because the source estate mixes personal and work sessions.
  Empty allowlist → no HTTP request at all.
- **The freshness statement is never borrowed from an untrusted frame.**
  Unreadable and foreign-publisher frames are counted but never timed, so a
  corrupt or hostile frame cannot make a stale answer look fresh (D9).

Two interpretations of the delta are recorded here because the sentences alone do
not settle them:

1. G3's arithmetic ("3 snapshots were found ... (2 unreadable, 1 from an
   unexpected publisher)") counts unreadable frames among those *found*, so a
   poll of nothing but unreadable frames is G3, not G2. G2 therefore fires when
   no snapshot was found at all — an empty poll body, or one carrying only
   `open`/`keepalive` frames, which is exactly the shape probe 1.4 measured.
2. The caveat clause qualifies what was *shown*, so it applies when at least one
   session is listed (and neither the filtered nor the unlisted clause is
   already saying the result is partial). Without that condition the notes line
   could never be absent, and the delta requires that it can be.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

import httpx

from henk.tools.backend_failure import backend_failure_reason
from henk.tools.base import Tool, ToolClass, ToolResult

logger = logging.getLogger("henk.tools.sessions_read")

#: A snapshot is a candidate only when its `publisher` starts with this. A
#: constant, not configuration: it is the publisher's identity, and a config key
#: here would let a misconfiguration widen what counts as trusted.
PUBLISHER_PREFIX = "session-publisher/"

#: How much of a poll response is read before the read stops. D9's arithmetic:
#: six hours of one publish per 300-second tick is at most 72 frames of at most
#: 3.8 KB — about 274 KB — so the budget is generous by design and an overrun
#: means something other than the publisher is writing the topic.
READ_BUDGET_BYTES = 1_000_000

#: The only snapshot schema this build understands. A different value is
#: terminal (G4) with no fallback: an older candidate is not evidence that the
#: publisher and Henk agree.
SUPPORTED_SCHEMA = 1

#: herdr's documented status values. Anything else renders `status not
#: recognised` — the session is still listed, because its label and age are
#: shape-checked and useful.
KNOWN_STATUSES: frozenset[str] = frozenset(
    {"idle", "done", "working", "blocked", "unknown"}
)

#: The shape every rendered `pane` and `project` must match. Set from the
#: pane-id character set recorded by the live probe (notes/estate-probe.md §1.1),
#: not derived from herdr's documentation, which declines to specify a grammar.
VALUE_SHAPE = re.compile(r"^[\w:.-]{1,32}$")

#: Body ordering: blocked first, then working, then everything else; ascending
#: age within each group, with an unknown age last (D10).
_STATUS_RANK: dict[str, int] = {"blocked": 0, "working": 1}

_UNITS: tuple[tuple[int, str], ...] = (
    (86400, "day"),
    (3600, "hour"),
    (60, "minute"),
    (1, "second"),
)

_MISSING = object()


# -- humanising -------------------------------------------------------------


def humanise_seconds(seconds: float) -> str:
    """`N seconds/minutes/hours/days`, singular at one, truncating downwards."""
    total = max(0, int(seconds))
    for size, unit in _UNITS:
        if total >= size or size == 1:
            count = total // size
            return f"{count} {unit}" if count == 1 else f"{count} {unit}s"
    raise AssertionError("unreachable")  # pragma: no cover


def _sessions_were(count: int) -> str:
    """`1 session was` / `N sessions were` — the subject of four sentences."""
    return "1 session was" if count == 1 else f"{count} sessions were"


def _format_server_time(server_time: float | None) -> str:
    if server_time is None:
        return "an unrecorded time"
    moment = datetime.fromtimestamp(float(server_time), tz=timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


# -- the gate ---------------------------------------------------------------

G0_TEXT = (
    "No sessions are in scope: the allowlist "
    "(personal_data.session_project_allowlist) has no entries, so nothing from "
    "the workstation can be surfaced."
)


def render_no_snapshot(lookback_seconds: int) -> str:
    return (
        "The workstation has not published a session snapshot in the last "
        f"{humanise_seconds(lookback_seconds)}."
    )


def render_no_candidates(found: int, unreadable: int, foreign: int) -> str:
    return (
        f"{found} snapshots were found in the poll, but none could be used "
        f"({unreadable} unreadable, {foreign} from an unexpected publisher)."
    )


def render_unrecognised_schema(value: Any = _MISSING) -> str:
    if value is _MISSING:
        rendered = "missing"
    elif isinstance(value, bool) or not isinstance(value, int):
        rendered = "unrecognised"
    else:
        rendered = str(value)
    return (
        f"The newest snapshot uses an unrecognised snapshot schema ({rendered}); "
        "the publisher and Henk are out of step."
    )


# -- part 1, the headline ---------------------------------------------------


def render_headline_fresh(age_seconds: float) -> str:
    return f"Workstation reported {humanise_seconds(age_seconds)} ago."


def render_headline_stale(age_seconds: float) -> str:
    return (
        f"Last snapshot is {humanise_seconds(age_seconds)} old; the workstation "
        "is probably asleep or the publisher has stopped (on the workstation: "
        "systemctl --user status session-publisher.timer)."
    )


def render_headline_unknown(server_time: float | None) -> str:
    return (
        "Freshness unknown; the server received this snapshot at "
        f"{_format_server_time(server_time)}."
    )


# -- part 2, the body -------------------------------------------------------


def render_body_none_shown(reported: int) -> str:
    return f"{_sessions_were(reported)} reported, but none could be shown."


def render_body_no_sessions() -> str:
    return "No live sessions."


def render_status(status: Any) -> str:
    if isinstance(status, str) and status in KNOWN_STATUSES:
        return status
    return "status not recognised"


def render_age(age_s: Any) -> str:
    if not _is_count(age_s):
        return "age unknown"
    return f"{humanise_seconds(age_s)} ago"


def render_session_line(project: str, status: Any, age_s: Any) -> str:
    """One listed session: label, status, age. Two spaces, no free text."""
    return f"{project}  {render_status(status)}  {render_age(age_s)}"


# -- part 3, the notes ------------------------------------------------------


def clause_ages() -> str:
    return "last-activity ages were unavailable on the workstation"


def clause_degraded(dropped: int | None) -> str:
    if dropped is None:
        return "sessions were dropped for size"
    return f"{_sessions_were(dropped)} dropped for size"


def clause_filtered(count: int) -> str:
    return f"{_sessions_were(count)} filtered by Henk's allowlist"


def clause_unusable(count: int) -> str:
    possessive = "its" if count == 1 else "their"
    return (
        f"{_sessions_were(count)} dropped because {possessive} fields were "
        "unusable (the publisher may be broken or something else may be writing "
        "the topic)"
    )


def clause_unlisted(count: int | None, blocked: int | None) -> str:
    if count is None or blocked is None:
        return "further sessions not shared"
    return f"{count} further sessions not shared ({blocked} blocked)"


def clause_cut_short() -> str:
    return (
        "the poll was cut short at the read budget, so a newer snapshot may exist"
    )


def clause_skipped(unreadable: int, foreign: int) -> str:
    return (
        f"{unreadable} unreadable and {foreign} unexpected-publisher snapshots "
        "were skipped"
    )


def clause_drift() -> str:
    return (
        "the publisher's heartbeat exceeds the staleness bound, so the staleness "
        "statement may be premature"
    )


def clause_no_heartbeat() -> str:
    return (
        "the publisher does not report its heartbeat, so the staleness statement "
        "may be premature"
    )


def clause_caveat() -> str:
    return "only allowlisted sessions are shared, and there may be others"


#: Row name → the literal substring by which a test pins that row's sentence
#: (design D10). The per-session line is deliberately absent: it is
#: pass-through, and the marker tests assert instead that no marker appears in
#: it.
MARKERS: dict[str, str] = {
    "gate.g0": "has no entries",
    "gate.g1": "ntfy",
    "gate.g2": "has not published a session snapshot",
    "gate.g3": "none could be used",
    "gate.g4": "unrecognised snapshot schema",
    "headline.fresh": "Workstation reported",
    "headline.stale": "probably asleep",
    "headline.unknown": "Freshness unknown",
    "body.none_shown": "none could be shown",
    "body.no_sessions": "No live sessions",
    "notes.ages": "ages were unavailable",
    "notes.degraded": "dropped for size",
    "notes.filtered": "filtered by Henk's allowlist",
    "notes.unusable": "fields were unusable",
    "notes.unlisted": "further sessions not shared",
    "notes.cut_short": "poll was cut short",
    "notes.skipped": "snapshots were skipped",
    "notes.drift": "heartbeat exceeds",
    "notes.no_heartbeat": "does not report its heartbeat",
    "notes.caveat": "there may be others",
}

#: The same functions the tool renders with, called with representative
#: interpolations. There is deliberately no second copy of any sentence here:
#: a marker test that compared literals to literals would pass with the tool's
#: own wording changed underneath it.
RENDERINGS: dict[str, Callable[[], str]] = {
    "gate.g0": lambda: G0_TEXT,
    "gate.g1": lambda: backend_failure_reason(
        "ntfy", httpx.TimeoutException("timed out"), timeout=10.0
    ),
    "gate.g2": lambda: render_no_snapshot(21600),
    "gate.g3": lambda: render_no_candidates(3, 2, 1),
    "gate.g4": lambda: render_unrecognised_schema(2),
    "headline.fresh": lambda: render_headline_fresh(240),
    "headline.stale": lambda: render_headline_stale(10800),
    "headline.unknown": lambda: render_headline_unknown(1788345600.0),
    "body.none_shown": lambda: render_body_none_shown(5),
    "body.no_sessions": render_body_no_sessions,
    "notes.ages": clause_ages,
    "notes.degraded": lambda: clause_degraded(2),
    "notes.filtered": lambda: clause_filtered(2),
    "notes.unusable": lambda: clause_unusable(1),
    "notes.unlisted": lambda: clause_unlisted(3, 1),
    "notes.cut_short": clause_cut_short,
    "notes.skipped": lambda: clause_skipped(1, 0),
    "notes.drift": clause_drift,
    "notes.no_heartbeat": clause_no_heartbeat,
    "notes.caveat": clause_caveat,
}


# -- shape helpers ----------------------------------------------------------


def _is_count(value: Any) -> bool:
    """A non-negative integer, and not a bool wearing one's clothes."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _count_field(container: Any, key: str) -> int | None:
    if not isinstance(container, dict):
        return None
    value = container.get(key)
    return value if _is_count(value) else None


def _shape_ok(value: Any) -> bool:
    return isinstance(value, str) and VALUE_SHAPE.fullmatch(value) is not None


def _parse_generated_at(value: Any) -> float | None:
    """ISO-8601 UTC → epoch seconds. `Z` accepted; a naive stamp is read as UTC."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _order_key(entry: dict[str, Any]) -> tuple[int, int, int]:
    status = entry.get("status")
    rank = _STATUS_RANK.get(status, 2) if isinstance(status, str) else 2
    age = entry.get("age_s")
    if not _is_count(age):
        return (rank, 1, 0)
    return (rank, 0, age)


@dataclass
class _PollState:
    """What the stream parser accumulates, across both stages."""

    unreadable: int = 0
    foreign: int = 0
    candidates: int = 0
    cut_short: bool = False
    snapshot: dict[str, Any] | None = None
    server_time: float | None = None
    #: Sort key of the kept candidate. Frames with no usable `time` sort below
    #: every timed frame rather than winning by arriving last.
    key: float = float("-inf")

    @property
    def found(self) -> int:
        """Snapshots the poll turned up, usable or not — G2 vs G3 turns on this."""
        return self.unreadable + self.foreign + self.candidates


class SessionsReadTool(Tool):
    name = "sessions_read"
    description = (
        "The owner's Claude Code sessions on the workstation, as last reported "
        "by the workstation publisher. Read-only, takes no arguments. Every "
        "result states how old the report is; listed sessions are only the "
        "allowlisted ones and are never all sessions."
    )
    tool_class = ToolClass.READ_ONLY
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        topic: str,
        token: str = "",
        timeout: float = 10.0,
        allowlist: Sequence[str] = (),
        stale_after_seconds: int = 1500,
        lookback_seconds: int = 21600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._base_url = (base_url or "").rstrip("/")
        self._topic = topic
        self._token = token
        self._timeout = float(timeout)
        # The loader owns the strip-and-discard rule (`normalise_label_allowlist`,
        # henk/config.py:1087) so the gate cannot re-derive it slightly
        # differently. All that happens here is a refusal to let a non-string or
        # an empty entry through, which can only arrive from a caller that
        # bypassed the loader — and an empty entry must never broaden scope.
        self._allowlist = tuple(
            entry for entry in (allowlist or ()) if isinstance(entry, str) and entry
        )
        self._stale_after = int(stale_after_seconds)
        self._lookback = int(lookback_seconds)
        self._clock = clock

    @property
    def effective_allowlist(self) -> tuple[str, ...]:
        """Surviving allowlist entries. Empty → the tool surfaces nothing (G0)."""
        return self._allowlist

    # -- selection --------------------------------------------------------

    async def _run(self, **arguments: Any) -> ToolResult:  # type: ignore[override]
        if not self._allowlist:
            return ToolResult.success(G0_TEXT)

        state = _PollState()
        windows = [self._stale_after]
        if self._lookback != self._stale_after:
            windows.append(self._lookback)
        for window in windows:
            try:
                await self._poll(window, state)
            except httpx.HTTPError as exc:
                return ToolResult.failure(
                    backend_failure_reason("ntfy", exc, timeout=self._timeout)
                )
            if state.snapshot is not None:
                break

        if state.snapshot is None:
            if state.found == 0:
                return ToolResult.success(render_no_snapshot(self._lookback))
            return ToolResult.success(
                render_no_candidates(state.found, state.unreadable, state.foreign)
            )

        snapshot = state.snapshot
        schema = snapshot.get("schema", _MISSING)
        if not _is_count(schema) or schema != SUPPORTED_SCHEMA:
            return ToolResult.success(render_unrecognised_schema(schema))

        return ToolResult.success(self._render(snapshot, state))

    async def _poll(self, window: int, state: _PollState) -> None:
        """One `poll=1` GET, stream-parsed under the read budget."""
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        url = f"{self._base_url}/{self._topic}/json"
        params = {"poll": "1", "since": f"{int(window)}s"}
        consumed = 0
        buffer = b""
        async with self._client.stream(
            "GET", url, params=params, headers=headers, timeout=self._timeout
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if consumed + len(chunk) > READ_BUDGET_BYTES:
                    # Truncate rather than finish the chunk: the budget must bind
                    # regardless of how the transport happened to frame the body,
                    # or a single-chunk response would defeat it.
                    chunk = chunk[: READ_BUDGET_BYTES - consumed]
                    state.cut_short = True
                consumed += len(chunk)
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._consume(line, state)
                if state.cut_short:
                    # The tail is a partial line; parsing it would count a
                    # truncation as a corrupt frame.
                    buffer = b""
                    break
            if buffer.strip():
                self._consume(buffer, state)

    def _consume(self, raw: bytes, state: _PollState) -> None:
        """Classify one stream line: ignored, unreadable, foreign, or candidate."""
        text = raw.strip()
        if not text:
            return
        try:
            frame = json.loads(text)
        except (ValueError, UnicodeDecodeError):
            state.unreadable += 1
            return
        if not isinstance(frame, dict):
            state.unreadable += 1
            return
        if frame.get("event") != "message":
            # `open` and `keepalive` are ntfy's own bookkeeping; they are not
            # snapshots and are not counted as any kind of frame.
            return

        body = frame.get("message")
        parsed: Any = None
        if isinstance(body, str):
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
        if not isinstance(parsed, dict):
            state.unreadable += 1
            return

        publisher = parsed.get("publisher")
        if not isinstance(publisher, str) or not publisher.startswith(
            PUBLISHER_PREFIX
        ):
            state.foreign += 1
            return

        state.candidates += 1
        when = frame.get("time")
        timed = isinstance(when, (int, float)) and not isinstance(when, bool)
        server_time = float(when) if timed else None
        key = server_time if server_time is not None else float("-inf")
        if state.snapshot is None or key > state.key:
            state.snapshot = parsed
            state.server_time = server_time
            state.key = key

    # -- rendering --------------------------------------------------------

    def _render(self, snapshot: dict[str, Any], state: _PollState) -> str:
        raw_sessions = snapshot.get("sessions")
        sessions = raw_sessions if isinstance(raw_sessions, list) else []

        listed: list[dict[str, Any]] = []
        filtered = 0
        unusable = 0
        for entry in sessions:
            if not isinstance(entry, dict):
                unusable += 1
                continue
            if not _shape_ok(entry.get("pane")) or not _shape_ok(
                entry.get("project")
            ):
                unusable += 1
                continue
            if entry["project"] not in self._allowlist:
                filtered += 1
                continue
            listed.append(entry)
        reported = len(sessions)

        lines = [self._headline(snapshot, state)]
        if listed:
            lines.extend(
                render_session_line(
                    entry["project"], entry.get("status"), entry.get("age_s")
                )
                for entry in sorted(listed, key=_order_key)
            )
        elif reported == 0:
            lines.append(render_body_no_sessions())
        else:
            lines.append(render_body_none_shown(reported))

        notes = self._notes(snapshot, state, filtered, unusable, len(listed))
        if notes:
            lines.append(f"Notes: {'; '.join(notes)}")
        return "\n".join(lines)

    def _headline(self, snapshot: dict[str, Any], state: _PollState) -> str:
        generated = _parse_generated_at(snapshot.get("generated_at"))
        if generated is None:
            return render_headline_unknown(state.server_time)
        age = self._clock() - generated
        if age < 0:
            # A snapshot from the future is a clock disagreement, not freshness.
            return render_headline_unknown(state.server_time)
        if age <= self._stale_after:
            return render_headline_fresh(age)
        return render_headline_stale(age)

    def _notes(
        self,
        snapshot: dict[str, Any],
        state: _PollState,
        filtered: int,
        unusable: int,
        listed: int,
    ) -> list[str]:
        clauses: list[str] = []
        if snapshot.get("age_source") == "none":
            clauses.append(clause_ages())
        if "degraded" in snapshot:
            clauses.append(clause_degraded(_count_field(snapshot["degraded"], "dropped")))
        if filtered:
            clauses.append(clause_filtered(filtered))
        if unusable:
            clauses.append(clause_unusable(unusable))
        unlisted_present = "unlisted" in snapshot
        if unlisted_present:
            unlisted = snapshot["unlisted"]
            clauses.append(
                clause_unlisted(
                    _count_field(unlisted, "count"), _count_field(unlisted, "blocked")
                )
            )
        if state.cut_short:
            clauses.append(clause_cut_short())
        if state.unreadable or state.foreign:
            clauses.append(clause_skipped(state.unreadable, state.foreign))
        heartbeat = snapshot.get("heartbeat_s")
        tick = snapshot.get("tick_s")
        if _is_count(heartbeat) and _is_count(tick):
            if heartbeat + 2 * tick > self._stale_after:
                clauses.append(clause_drift())
        else:
            clauses.append(clause_no_heartbeat())
        if listed and not filtered and not unlisted_present:
            clauses.append(clause_caveat())
        return clauses
