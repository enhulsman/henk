"""Audit-log tests (task 2.4), from specs/audit-log.

Every record built by the app is validated against the committed JSON Schema
(the transferable artifact). Also covered: schema_version presence, session and
suppression record shapes, triage_arc_complete on event-triggered records,
append-only behaviour, and that a failed write is loud but non-blocking.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from henk.audit import (
    AUDIT_SCHEMA_PATH,
    AUDIT_SCHEMA_V1_PATH,
    SCHEMA_VERSION,
    AuditLog,
    session_record,
    suppression_record,
)

SCHEMA = json.loads(AUDIT_SCHEMA_PATH.read_text())


def _validate(record: dict) -> None:
    jsonschema.validate(record, SCHEMA)


# --- Schema is a published, versioned artifact ----------------------------


def test_every_record_carries_schema_version():
    assert session_record(trigger="owner-message")["schema_version"] == SCHEMA_VERSION
    assert suppression_record(identity_key="k", reason="cooldown")[
        "schema_version"
    ] == SCHEMA_VERSION


def test_owner_session_record_validates():
    rec = session_record(trigger="owner-message", turn_count=2, model="claude-sonnet-5")
    _validate(rec)
    assert rec["trigger"] == "owner-message"
    assert rec["triage_arc_complete"] is None  # not an event session


def test_event_session_record_validates_with_arc_flag():
    rec = session_record(
        trigger="event",
        event=[{"identity_key": "gatus:svc/api", "source": "gatus", "state": "firing"}],
        tool_calls=[{"name": "homelab_health", "tool_class": "read-only"}],
        diagnosis="ETL stalled",
        confidence="moderate",
        handoff_message_id="hf-1",
        triage_arc_complete=True,
        announceable=True,
    )
    _validate(rec)
    assert rec["triage_arc_complete"] is True
    assert rec["handoff_message_id"] == "hf-1"


# --- Schema v2: per-triage semantics + cache-read usage -------------------


def test_schema_version_is_two():
    assert SCHEMA_VERSION == 2  # bumped for per-triage records + cache-read usage


def test_new_records_declare_v2_and_validate():
    rec = session_record(
        trigger="event",
        usage={"input_tokens": 4, "output_tokens": 200, "cache_read_input_tokens": 800},
    )
    _validate(rec)  # against the v2 schema (AUDIT_SCHEMA_PATH)
    assert rec["schema_version"] == 2


def test_usage_carries_cache_read_in_v2():
    rec = session_record(
        trigger="event",
        usage={"input_tokens": 4, "output_tokens": 2, "cache_read_input_tokens": 90},
    )
    assert rec["usage"]["cache_read_input_tokens"] == 90
    _validate(rec)


def test_old_v1_records_still_validate_against_v1_schema():
    # Historical records keep declaring version 1 and must validate against the
    # committed v1 schema (readers stay valid across the bump).
    v1_schema = json.loads(AUDIT_SCHEMA_V1_PATH.read_text())
    v1_record = {
        "schema_version": 1,
        "record_type": "session",
        "trigger": "event",
        "outcome": "completed",
        "tool_calls": [{"name": "homelab_health", "tool_class": "read-only"}],
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "at": 1.0,
    }
    jsonschema.validate(v1_record, v1_schema)
    assert v1_schema["properties"]["schema_version"]["const"] == 1


def test_suppression_record_validates():
    rec = suppression_record(
        identity_key="grafana:HenkSwapPressure", reason="cooldown", event_id="e1"
    )
    _validate(rec)
    assert rec["record_type"] == "suppression"


def test_session_record_missing_required_field_fails_validation():
    bad = session_record(trigger="event")
    del bad["outcome"]  # required for session records
    with pytest.raises(jsonschema.ValidationError):
        _validate(bad)


# --- Writer: append-only, non-blocking ------------------------------------


def test_write_appends_one_line_per_record(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    assert log.write(session_record(trigger="owner-message")) is True
    assert log.write(suppression_record(identity_key="k", reason="cooldown")) is True
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["record_type"] == "session"
    assert json.loads(lines[1])["record_type"] == "suppression"


def test_write_never_modifies_prior_records(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write(session_record(trigger="owner-message", turn_count=1))
    first_line = (tmp_path / "audit.jsonl").read_text().splitlines()[0]
    log.write(session_record(trigger="event", outcome="completed"))
    after = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert after[0] == first_line  # earlier record untouched (append-only)
    assert len(after) == 2


def test_write_stamps_at_when_absent(tmp_path: Path):
    log = AuditLog(tmp_path / "audit.jsonl", clock=lambda: 123.0)
    log.write({"schema_version": 1, "record_type": "suppression",
               "identity_key": "k", "reason": "cooldown"})
    rec = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert rec["at"] == 123.0


def test_write_stamps_at_when_present_but_none(tmp_path: Path):
    # The builders always emit "at": None, so setdefault (key present) never
    # stamps — every production record carried at: null. Stamp on None too.
    log = AuditLog(tmp_path / "audit.jsonl", clock=lambda: 123.0)
    log.write(session_record(trigger="owner-message"))
    log.write(suppression_record(identity_key="k", reason="cooldown"))
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["at"] == 123.0
    assert json.loads(lines[1])["at"] == 123.0


def test_write_preserves_explicit_at(tmp_path: Path):
    # A caller-supplied timestamp must survive (never overwritten by the clock).
    log = AuditLog(tmp_path / "audit.jsonl", clock=lambda: 123.0)
    log.write(session_record(trigger="event", at=999.0))
    rec = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert rec["at"] == 999.0


def test_write_failure_is_logged_not_raised(tmp_path: Path, caplog):
    # Point the log at a path whose parent is a FILE, so mkdir/open fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    log = AuditLog(blocker / "audit.jsonl")
    import logging

    with caplog.at_level(logging.ERROR, logger="henk.audit"):
        ok = log.write(session_record(trigger="owner-message"))
    assert ok is False  # non-blocking: returns False rather than raising
    assert any("audit write failed" in r.message for r in caplog.records)
