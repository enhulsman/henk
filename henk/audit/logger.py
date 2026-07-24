"""Non-blocking JSONL audit writer and record builders.

The writer appends one JSON object per line and never rewrites or truncates
(append-only). Every write is wrapped: a failure is logged at ERROR and swallowed
so triage, replies, and message handling are never blocked by the audit path
(audit-log spec). Records are built by :func:`session_record` /
:func:`suppression_record` so their field names match the committed JSON Schema.
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
SCHEMA_VERSION = 2

_SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

#: The current schema, matching :data:`SCHEMA_VERSION`. Historical versions stay
#: committed so records that declare an older version still validate (audit-log
#: spec: prior schema versions remain readable).
AUDIT_SCHEMA_PATH = _SCHEMA_DIR / "audit-record.v2.schema.json"
AUDIT_SCHEMA_V1_PATH = _SCHEMA_DIR / "audit-record.v1.schema.json"
AUDIT_SCHEMA_V2_PATH = AUDIT_SCHEMA_PATH


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
    outcome: str = "completed",
    announceable: bool | None = None,
    turn_count: int = 0,
    model: str | None = None,
    usage: Mapping[str, Any] | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """Build one session record. Field names/types match the v1 JSON Schema."""
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
