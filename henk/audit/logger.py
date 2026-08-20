"""Non-blocking JSONL audit writer and record builders.

The writer appends one JSON object per line and never rewrites or truncates
(append-only). Every write is wrapped: a failure is logged at ERROR and swallowed
so triage, replies, and message handling are never blocked by the audit path
(audit-log spec). Records are built by :func:`session_record`,
:func:`suppression_record` and :func:`authorization_record` so their field names
match the committed JSON Schema.

:class:`MutationReceipts` is the decision-time half: it appends an
``authorization`` record the moment the gate decides, without waiting for the turn
or session to end and without depending on a graceful close. An agent that acts
without asking must be more accountable, not less — so the receipt has to be
durable before the mutation's effects are visible anywhere else.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger("henk.audit")

#: Bump on any change to the record structure (audit-log spec: schema is versioned).
#: v2: event-triage records are one-per-triage (was one-per-session) and `usage`
#: gains `cache_read_input_tokens`.
#: v3: the `authorization` record type (mutation receipts, model-initiated and
#: owner-command), the tier+outcome shape of session `approvals` entries (v2 used
#: `decision`), the `executed` flag on `tool_calls`, and `memory_hash`.
#: v4: the `reminder` record type (one per lifecycle transition) and the `scheduler`
#: value for `initiated_by`. v4 declares the COMPLETE reminder transition
#: enumeration, delivery's half included, so shipping `reminder-delivery` needs no
#: further bump — a schema document is a validation contract, not an inventory of
#: what the current build emits.
SCHEMA_VERSION = 4

_SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

#: The current schema, matching :data:`SCHEMA_VERSION`. Historical versions stay
#: committed so records that declare an older version still validate (audit-log
#: spec: prior schema versions remain readable).
AUDIT_SCHEMA_PATH = _SCHEMA_DIR / "audit-record.v4.schema.json"
AUDIT_SCHEMA_V1_PATH = _SCHEMA_DIR / "audit-record.v1.schema.json"
AUDIT_SCHEMA_V2_PATH = _SCHEMA_DIR / "audit-record.v2.schema.json"
AUDIT_SCHEMA_V3_PATH = _SCHEMA_DIR / "audit-record.v3.schema.json"
AUDIT_SCHEMA_V4_PATH = AUDIT_SCHEMA_PATH

#: Owner-command receipts carry a bounded effect summary — a receipt is evidence,
#: not a transcript, and the audit log is not a place to spill free text.
DETAIL_MAX_CHARS = 200


def suppression_record(
    *, identity_key: str, reason: str, event_id: str = "", at: float | None = None
) -> dict[str, Any]:
    """Build a suppression record (an event dropped by cooldown or the cap)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "suppression",
        "identity_key": identity_key,
        "reason": reason,
        "event_id": event_id,
        "at": at,
    }


def authorization_record(
    *,
    tool: str,
    outcome: str,
    tier: str | None = None,
    reference: str | None = None,
    turn_type: str | None = None,
    initiated_by: str = "model",
    detail: str | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Build one mutation receipt (audit-log spec: durable at decision time).

    Records **authorization**, never execution: the gate cannot know whether the
    tool then ran. Execution evidence lives in the session record's ``tool_calls``
    ``executed`` flag, which is derived by correlating with these records.

    ``tool`` is the named action — a registered tool name for model-initiated
    decisions, the command itself (``/forget``) for owner-command receipts, which
    carry ``tier: None`` and ``turn_type: "command"`` because they run outside any
    turn or session.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "authorization",
        "tool": tool,
        "tier": tier,
        "outcome": outcome,
        "reference": reference,
        "turn_type": turn_type,
        "initiated_by": initiated_by,
        "detail": _bounded(detail),
        "at": at,
    }


#: Every reminder lifecycle transition v4 can express. The four this change never
#: writes belong to `reminder-delivery`; they are declared so that half needs no
#: schema bump. `reinstated` is a TRANSITION name, not a row status — a reinstated
#: reminder's stored status is `pending`.
REMINDER_TRANSITIONS = (
    "scheduled",
    "cancelled",
    "reinstated",
    "delivered",
    "delivered-late",
    "missed",
    "abandoned",
)

#: Who caused a transition. `scheduler` is delivery's; nothing here emits it.
REMINDER_INITIATORS = ("model", "owner-command", "scheduler")


def reminder_record(
    *,
    reminder_id: int,
    due_at: float | None,
    transition: str,
    initiated_by: str = "model",
    at: float | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build one reminder lifecycle record (audit-log spec).

    Carries the reminder's id, its due **instant**, the transition, the initiator and
    a timestamp — and deliberately **not** the reminder's text. The store holds the
    content; the log holds the evidence, and the log gets read and pasted around, so
    owner-personal free text does not belong in it. The committed v4 document
    enforces that as well as this builder does.

    The due time is the epoch instant rather than a rendered string: rendering depends
    on the currently configured zone, and a receipt must not move when the config does.

    This record is appended **after** the store transaction that performed the
    transition commits, so the log never claims a transition the store did not make.
    A crash between the two costs a receipt for a real transition, which is the
    preferable direction: a log that claims state the store does not have is worse
    than a log with a gap. That ordering is the caller's to honour — see
    :class:`ReminderReceipts`.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "reminder",
        "reminder_id": int(reminder_id),
        "due_at": None if due_at is None else float(due_at),
        "transition": transition,
        "initiated_by": initiated_by,
        "detail": _bounded(detail),
        "at": at,
    }


class ReminderReceipts:
    """Appends one `reminder` record per lifecycle transition. Never blocks.

    A thin counterpart to :class:`MutationReceipts`, deliberately separate because the
    two answer different questions at different moments: an `authorization` record
    says whether the agent was *permitted* to act and is written when the gate
    decides; a `reminder` record says what *changed* and is written after the store
    commits. One existing without the other is itself evidence — an authorization with
    no transition means the tool was allowed and then failed.

    Call this only on a transition that actually happened. A scheduling, cancellation
    or reinstatement rejected by validation, by the cap, or by an unknown id writes
    **nothing**: receipts record state changes, and none occurred.
    """

    def __init__(self, audit: "AuditLog | None") -> None:
        self._audit = audit

    def record(
        self,
        *,
        reminder_id: int,
        due_at: float | None,
        transition: str,
        initiated_by: str = "model",
        detail: str | None = None,
    ) -> dict[str, Any]:
        record = reminder_record(
            reminder_id=reminder_id,
            due_at=due_at,
            transition=transition,
            initiated_by=initiated_by,
            detail=detail,
        )
        if self._audit is not None:
            self._audit.write(record)  # loud but non-blocking; never raises
        return record


def _bounded(detail: str | None) -> str | None:
    if detail is None:
        return None
    text = " ".join(str(detail).split())
    if len(text) > DETAIL_MAX_CHARS:
        return text[:DETAIL_MAX_CHARS] + "…"
    return text


def approval_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project an authorization record into a session record's ``approvals`` entry."""
    return {
        "tool": record["tool"],
        "tier": record.get("tier"),
        "outcome": record["outcome"],
        "initiated_by": record.get("initiated_by", "model"),
        "reference": record.get("reference"),
    }


class MutationReceipts:
    """Makes every authorization decision durable the moment it is made.

    Wired as the gate's decision recorder and as the owner-command dispatch's
    receipt writer. ``sink`` (the agent core) additionally collects model-initiated
    entries for the session record's ``approvals`` — but the durable half never
    depends on the sink, so a session that dies before its record still leaves the
    decision on disk.
    """

    def __init__(self, audit: "AuditLog | None", *, sink=None) -> None:
        self._audit = audit
        self.sink = sink

    def record(
        self,
        *,
        tool: str,
        outcome: str,
        tier: str | None = None,
        reference: str | None = None,
        turn_type: str | None = None,
        initiated_by: str = "model",
        detail: str | None = None,
    ) -> dict[str, Any]:
        record = authorization_record(
            tool=tool,
            outcome=outcome,
            tier=tier,
            reference=reference,
            turn_type=turn_type,
            initiated_by=initiated_by,
            detail=detail,
        )
        if self._audit is not None:
            self._audit.write(record)  # loud but non-blocking; never raises
        if self.sink is not None:
            try:
                self.sink(record)
            except Exception:
                # The durable receipt already exists; an aggregation failure must
                # not propagate into the turn that is being authorized.
                logger.error("could not aggregate an authorization receipt",
                             exc_info=True)
        return approval_entry(record)


def session_record(
    *,
    trigger: str,
    event: Sequence[Mapping[str, Any]] | None = None,
    tool_calls: Iterable[Mapping[str, Any]] = (),
    diagnosis: str | None = None,
    confidence: str | None = None,
    handoff_message_id: str | None = None,
    triage_arc_complete: bool | None = None,
    approvals: Iterable[Mapping[str, Any]] = (),
    memory_hash: str | None = None,
    outcome: str = "completed",
    announceable: bool | None = None,
    turn_count: int = 0,
    model: str | None = None,
    usage: Mapping[str, Any] | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Build one session record. Field names/types match the current JSON Schema."""
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "session",
        "trigger": trigger,
        "event": list(event) if event is not None else None,
        "tool_calls": [dict(c) for c in tool_calls],
        "diagnosis": diagnosis,
        "confidence": confidence,
        "handoff_message_id": handoff_message_id,
        "triage_arc_complete": triage_arc_complete,
        "approvals": [dict(a) for a in approvals],
        "memory_hash": memory_hash,
        "outcome": outcome,
        "announceable": announceable,
        "turn_count": turn_count,
        "model": model,
        "usage": dict(usage) if usage is not None else None,
        "at": at,
    }


def read_audit_records(
    path: str | Path, *, limit: int | None = None
) -> list[dict[str, Any]]:
    """Read persisted audit records for cadence rehydration (design D2).

    Returns the parsed JSONL records (optionally only the last ``limit``, a
    bounded tail read for large logs). Malformed lines are skipped, and a
    missing/unreadable file yields ``[]`` — rehydration is best-effort and must
    never crash startup.
    """
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if limit is not None:
        lines = lines[-limit:]
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("skipping malformed audit line during rehydration")
    return records


class AuditLog:
    """Appends records to a JSONL file. Never raises on write failure."""

    def __init__(self, path: str | Path, *, clock=time.time) -> None:
        self._path = Path(path)
        self._clock = clock

    def write(self, record: Mapping[str, Any]) -> bool:
        """Append one record. Returns True on success, False (logged) on failure."""
        payload = dict(record)
        # The builders always emit "at": None, so setdefault (key present) would
        # never stamp — stamp whenever it is missing or None; keep any explicit value.
        if payload.get("at") is None:
            payload["at"] = self._clock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return True
        except OSError:
            # Loud but non-blocking: the caller continues regardless (design D8).
            logger.error("audit write failed for %s", self._path, exc_info=True)
            return False
