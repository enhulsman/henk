"""Append-only, schema-versioned audit log — the Anamata-transferable artifact.

One JSONL record per agent session (owner or event triggered), standalone
suppression records for events dropped by cooldown/cap, one ``authorization``
record per mutation decision — written at decision time, so a receipt never
depends on a graceful session close or on event intake being enabled — and one
``reminder`` record per reminder lifecycle transition, written after the store
transaction commits so the log never claims a transition the store did not make. The *application* writes
these, never the model (design D8). The record schema is a committed JSON Schema
document (:mod:`henk.audit.schema`) so another project can validate its own
records against it. Audit write failures are loud (ERROR) but never block
message handling — availability of triage beats completeness of audit.
"""

from henk.audit.logger import (
    AUDIT_SCHEMA_PATH,
    AUDIT_SCHEMA_V1_PATH,
    AUDIT_SCHEMA_V2_PATH,
    AUDIT_SCHEMA_V3_PATH,
    AUDIT_SCHEMA_V4_PATH,
    DETAIL_MAX_CHARS,
    SCHEMA_VERSION,
    AuditLog,
    REMINDER_INITIATORS,
    REMINDER_TRANSITIONS,
    MutationReceipts,
    ReminderReceipts,
    approval_entry,
    authorization_record,
    read_audit_records,
    reminder_record,
    session_record,
    suppression_record,
)

__all__ = [
    "AUDIT_SCHEMA_PATH",
    "AUDIT_SCHEMA_V1_PATH",
    "AUDIT_SCHEMA_V2_PATH",
    "AUDIT_SCHEMA_V3_PATH",
    "AUDIT_SCHEMA_V4_PATH",
    "DETAIL_MAX_CHARS",
    "MutationReceipts",
    "REMINDER_INITIATORS",
    "REMINDER_TRANSITIONS",
    "ReminderReceipts",
    "SCHEMA_VERSION",
    "AuditLog",
    "approval_entry",
    "authorization_record",
    "read_audit_records",
    "reminder_record",
    "session_record",
    "suppression_record",
]
