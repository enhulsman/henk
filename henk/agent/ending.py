"""How a triage turn ended, and the honest notice when it did not complete (D12).

The classifier reads the agent SDK's **structured signals first** and considers the
reply text only after all of them. The first matching check decides:

1. an assistant-message error in the turn: ``error``;
2. a ``refusal`` stop reason on an assistant or the result message: ``refused``;
3. the result message marked as an error, carrying an API error status, or
   reporting a terminal reason other than ``completed``: ``error``;
4. the turn raised: ``error``;
5. reply text that is a CLI-rendered API error (``API Error…``): ``error``;
6. empty reply text: ``no-reply``;
7. otherwise ``completed``.

For any ending other than ``completed`` the reply text is discarded, and an
announceable incident gets :func:`incomplete_triage_notice` instead. The notice is
application-authored: it carries no refusal category (the SDK reports none), no
model text, and no exception text. Its only SDK-derived parts are the closed
assistant-error class and a validated HTTP status.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from henk.agent.session import TurnEnding
from henk.agent.turns import EventTurn

#: The triage record's ``outcome`` values (audit-log v5 documents all four).
COMPLETED = "completed"
ERROR = "error"
REFUSED = "refused"
NO_REPLY = "no-reply"

#: ``AssistantMessageError`` in claude_agent_sdk 0.2.157 (``types.py:1039-1046``):
#: a closed, content-free enum. A value outside it is rendered as ``unknown``,
#: because the field is copied from the CLI's JSON frame unchecked.
ASSISTANT_ERROR_CLASSES = frozenset({
    "authentication_failed",
    "billing_error",
    "rate_limit",
    "invalid_request",
    "server_error",
    "unknown",
})
_UNKNOWN_CLASS = "unknown"

#: The one ``terminal_reason`` that means the query loop finished normally.
#: ``None`` (an older CLI, or no report) is no signal.
_TERMINAL_COMPLETED = "completed"

#: The CLI renders a failed API call as reply text beginning with this. It is a
#: heuristic, checked only after every structured signal.
_API_ERROR_PREFIX = "API Error"

#: The incident name in the notice, at most this many characters.
INCIDENT_NAME_MAX = 80
#: The whole notice, before any suppressed-incident note, at most this long.
NOTICE_MAX_CHARS = 320

_NOTICE_PREFIX = "[AI] Triage incomplete for "
_NO_DIAGNOSIS = "No diagnosis was produced."
_PICKUP_HANDOFF = "Pickup: run henk-pickup for the published handoff."
_PICKUP_AUDIT = "Pickup: no handoff was published; see the audit record for this incident."
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class TriageEnding:
    """A classified ending: the outcome, and what the notice may say about it."""

    outcome: str
    #: The closed assistant-error class, for an ``error`` ending that had one.
    error_class: str | None = None
    #: The HTTP status, for an ``error`` ending whose SDK result reported one.
    http_status: int | None = None


def _http_status(value: object) -> int | None:
    """``value`` if it is an HTTP status code, else None (never rendered).

    A ``bool`` is an ``int`` here, but only 0 or 1, so the range excludes it."""
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def _error(signals: TurnEnding | None) -> TriageEnding:
    error_class = None
    status = None
    if signals is not None:
        if signals.assistant_error:
            error_class = (
                signals.assistant_error
                if signals.assistant_error in ASSISTANT_ERROR_CLASSES
                else _UNKNOWN_CLASS
            )
        status = _http_status(signals.api_error_status)
    return TriageEnding(ERROR, error_class=error_class, http_status=status)


def classify_ending(
    signals: TurnEnding | None, *, raised: bool, reply: str | None
) -> TriageEnding:
    """Classify one triage turn's ending. ``signals`` None means none reported."""
    if signals is not None:
        if signals.assistant_error:
            return _error(signals)
        if signals.refusal:
            return TriageEnding(REFUSED)
        if (
            signals.result_is_error
            or signals.api_error_status is not None
            or (
                signals.terminal_reason is not None
                and signals.terminal_reason != _TERMINAL_COMPLETED
            )
        ):
            return _error(signals)
    if raised:
        return _error(signals)
    text = (reply or "").strip()
    if text.startswith(_API_ERROR_PREFIX):
        return _error(signals)
    if not text:
        return TriageEnding(NO_REPLY)
    return TriageEnding(COMPLETED)


def _incident_name(turn: EventTurn) -> str:
    """The first incident's name, flattened to one line and bounded, plus a
    count of the others in a storm. The name is payload-derived, so every
    whitespace run (newlines included) becomes one space: it cannot add a line."""
    first = turn.items[0].identity.name if turn.items else ""
    name = _WHITESPACE.sub(" ", first).strip() or "an unnamed incident"
    more = len(turn.items) - 1
    suffix = f" (+{more} more)" if more > 0 else ""
    room = INCIDENT_NAME_MAX - len(suffix)
    if len(name) > room:
        name = name[: room - 1].rstrip() + "…"
    return name + suffix


def _how(ending: TriageEnding) -> str:
    if ending.outcome == REFUSED:
        return "the model declined the request"
    if ending.outcome == NO_REPLY:
        return "the model produced no reply"
    parts = []
    if ending.error_class:
        parts.append(ending.error_class)
    if ending.http_status is not None:
        parts.append(f"HTTP {ending.http_status}")
    detail = f" ({', '.join(parts)})" if parts else ""
    return f"the triage failed with an error{detail}"


def incomplete_triage_notice(
    turn: EventTurn, ending: TriageEnding, *, handoff_published: bool
) -> str:
    """The owner-facing notice for a triage that did not complete (D12).

    Three lines, the last a ``Pickup:`` line, and no ``Diagnosis:`` or ``Fix:``
    line: there is no diagnosis, and a placeholder one would be false. The
    suppressed-incident note, when due, is appended by the core after it.
    """
    return "\n".join((
        f"{_NOTICE_PREFIX}{_incident_name(turn)}: {_how(ending)}.",
        _NO_DIAGNOSIS,
        _PICKUP_HANDOFF if handoff_published else _PICKUP_AUDIT,
    ))
