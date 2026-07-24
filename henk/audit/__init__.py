"""Append-only, schema-versioned audit log — the Anamata-transferable artifact.

One JSONL record per agent session (owner or event triggered), plus standalone
suppression records for events dropped by cooldown/cap. The *application* writes
these, never the model (design D8). The record schema is a committed JSON Schema
document (:mod:`henk.audit.schema`) so another project can validate its own
records against it. Audit write failures are loud (ERROR) but never block
message handling — availability of triage beats completeness of audit.
"""

from henk.audit.logger import (
    AUDIT_SCHEMA_PATH,
    AUDIT_SCHEMA_V1_PATH,
    AUDIT_SCHEMA_V2_PATH,
    SCHEMA_VERSION,
    AuditLog,
    read_audit_records,
    session_record,
    suppression_record,
)

__all__ = [
    "AUDIT_SCHEMA_PATH",
    "AUDIT_SCHEMA_V1_PATH",
    "AUDIT_SCHEMA_V2_PATH",
    "SCHEMA_VERSION",
    "AuditLog",
    "read_audit_records",
    "session_record",
    "suppression_record",
]
