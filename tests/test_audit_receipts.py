"""Mutation-receipt tests (task 3.1), from specs/audit-log.

Every mutating authorization decision leaves an `authorization` record on disk at
decision time — not at turn end, not at session close, and not conditional on
event intake being enabled. A standing tool that acts without asking is the reason
this has to be durable: the receipt is the only trace that the action was ever
authorized, so it must survive an OOM-kill between the write and the session's
record.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from henk.audit import (
    AUDIT_SCHEMA_PATH,
    AUDIT_SCHEMA_V1_PATH,
    AUDIT_SCHEMA_V2_PATH,
    AUDIT_SCHEMA_V3_PATH,
    SCHEMA_VERSION,
    AuditLog,
    MutationReceipts,
    approval_entry,
    authorization_record,
    session_record,
)

SCHEMA = json.loads(AUDIT_SCHEMA_PATH.read_text())


def _validate(record: dict) -> None:
    jsonschema.validate(record, SCHEMA)


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- The record shape -----------------------------------------------------


def test_authorization_record_carries_the_full_decision():
    rec = authorization_record(
        tool="capture",
        tier="standing",
        outcome="authorized",
        reference="appr-1",
        turn_type="owner",
    )
    _validate(rec)
    assert rec["record_type"] == "authorization"
    assert rec["schema_version"] == SCHEMA_VERSION
    assert rec["initiated_by"] == "model"
    assert (rec["tool"], rec["tier"], rec["outcome"]) == (
        "capture",
        "standing",
        "authorized",
    )


def test_owner_command_record_has_no_tier_and_a_command_turn_type():
    rec = authorization_record(
        tool="/forget",
        tier=None,
        outcome="authorized",
        turn_type="command",
        initiated_by="owner-command",
        detail="removed 2 memories",
    )
    _validate(rec)
    assert rec["tier"] is None  # tier is a TOOL property; commands have none
    assert rec["turn_type"] == "command"  # they run outside any turn or session
    assert rec["detail"] == "removed 2 memories"


@pytest.mark.parametrize(
    "outcome",
    [
        "authorized",
        "approved",
        "denied",
        "cancelled",
        "timeout",
        "suppressed",
        "out-of-scope",
        "rejected-busy",
    ],
)
def test_every_outcome_in_the_vocabulary_validates(outcome: str):
    _validate(authorization_record(tool="capture", tier="standing", outcome=outcome))


def test_an_invented_outcome_fails_validation():
    bad = authorization_record(tool="capture", tier="standing", outcome="probably-fine")
    with pytest.raises(jsonschema.ValidationError):
        _validate(bad)


def test_approval_entry_is_the_session_record_projection():
    rec = authorization_record(
        tool="store_memory", tier="standing", outcome="authorized", reference="appr-3"
    )
    entry = approval_entry(rec)
    assert entry == {
        "tool": "store_memory",
        "tier": "standing",
        "outcome": "authorized",
        "initiated_by": "model",
        "reference": "appr-3",
    }
    _validate(session_record(trigger="owner-message", approvals=[entry]))


# --- Durable at decision time --------------------------------------------


def test_recorder_appends_immediately(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    receipts = MutationReceipts(AuditLog(path))
    receipts.record(
        tool="capture", tier="standing", outcome="authorized", turn_type="owner"
    )
    # On disk already — no flush, no close, no session end.
    assert [r["record_type"] for r in _records(path)] == ["authorization"]
    assert _records(path)[0]["at"] is not None


def test_standing_receipt_survives_a_sigkill(tmp_path: Path):
    # The real hazard for a standing-tier write: the process dies between the
    # mutation and the session record. A graceful-close-dependent audit would lose
    # the only trace that the action was ever authorized.
    path = tmp_path / "audit.jsonl"
    script = (
        "import os, signal, sys;"
        "sys.path.insert(0, %r);"
        "from henk.audit import AuditLog, MutationReceipts;"
        "MutationReceipts(AuditLog(%r)).record("
        "tool='capture', tier='standing', outcome='authorized', turn_type='owner');"
        "os.kill(os.getpid(), signal.SIGKILL)"
    ) % (str(Path.cwd()), str(path))
    completed = subprocess.run([sys.executable, "-c", script])
    assert completed.returncode == -9  # genuinely killed, never unwound
    records = _records(path)
    assert len(records) == 1
    assert records[0]["tool"] == "capture"
    assert records[0]["outcome"] == "authorized"


def test_recorder_without_an_audit_log_still_returns_an_entry():
    # A reactive-only unit context has no audit; recording must stay a no-op that
    # never breaks the authorization path.
    entry = MutationReceipts(None).record(
        tool="capture", tier="standing", outcome="authorized"
    )
    assert entry["tool"] == "capture"


def test_recorder_fans_the_record_out_to_its_sink(tmp_path: Path):
    seen: list[dict] = []
    receipts = MutationReceipts(AuditLog(tmp_path / "audit.jsonl"), sink=seen.append)
    receipts.record(tool="capture", tier="standing", outcome="authorized")
    assert [r["tool"] for r in seen] == ["capture"]


def test_a_failing_sink_never_breaks_the_receipt(tmp_path: Path, caplog):
    import logging

    def boom(record):
        raise RuntimeError("sink exploded")

    path = tmp_path / "audit.jsonl"
    receipts = MutationReceipts(AuditLog(path), sink=boom)
    with caplog.at_level(logging.ERROR, logger="henk.audit"):
        receipts.record(tool="capture", tier="standing", outcome="authorized")
    assert len(_records(path)) == 1  # the durable half still happened


def test_a_failing_audit_write_is_reported_not_raised(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    receipts = MutationReceipts(AuditLog(blocker / "audit.jsonl"))
    # Loud but non-blocking, exactly like every other audit write.
    assert receipts.record(tool="capture", tier="standing", outcome="authorized")


# --- Schema version 4 -----------------------------------------------------


def test_schema_version_is_four():
    # The version pin, moved 3 -> 4 by reminders-core: v4 adds the `reminder`
    # record type and the `scheduler` initiator. This assertion exists so a bump
    # is always deliberate, which is exactly the service it performed here.
    assert SCHEMA_VERSION == 4


def test_v3_session_record_carries_executed_and_memory_hash():
    rec = session_record(
        trigger="owner-message",
        tool_calls=[
            {"name": "capture", "tool_class": "mutating", "executed": True},
            {"name": "homelab_health", "tool_class": "read-only", "executed": True},
        ],
        approvals=[
            {"tool": "capture", "tier": "standing", "outcome": "authorized"}
        ],
        memory_hash="abc123",
    )
    _validate(rec)
    assert rec["memory_hash"] == "abc123"
    assert rec["tool_calls"][0]["executed"] is True


def test_prior_schema_documents_remain_committed_and_valid():
    # "Schema is versioned" obliges every prior version's document to stay
    # committed so historical records validate against the version they declare.
    for path, version in (
        (AUDIT_SCHEMA_V1_PATH, 1),
        (AUDIT_SCHEMA_V2_PATH, 2),
        (AUDIT_SCHEMA_V3_PATH, 3),
    ):
        schema = json.loads(path.read_text())
        assert schema["properties"]["schema_version"]["const"] == version

    v1 = {
        "schema_version": 1,
        "record_type": "session",
        "trigger": "event",
        "outcome": "completed",
        "tool_calls": [{"name": "homelab_health", "tool_class": "read-only"}],
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "at": 1.0,
    }
    jsonschema.validate(v1, json.loads(AUDIT_SCHEMA_V1_PATH.read_text()))

    v2 = {
        "schema_version": 2,
        "record_type": "session",
        "trigger": "owner-message",
        "outcome": "completed",
        "tool_calls": [],
        "approvals": [{"tool": "spy", "decision": "approved"}],
        "at": 2.0,
    }
    jsonschema.validate(v2, json.loads(AUDIT_SCHEMA_V2_PATH.read_text()))

    v3 = {
        "schema_version": 3,
        "record_type": "authorization",
        "tool": "capture",
        "tier": "standing",
        "outcome": "authorized",
        "initiated_by": "model",
        "at": 3.0,
    }
    jsonschema.validate(v3, json.loads(AUDIT_SCHEMA_V3_PATH.read_text()))


def test_v3_rejects_a_v2_shaped_approvals_entry():
    # The approvals entry shape changed in v3 (tier + outcome, not `decision`),
    # which is exactly why the version had to increment.
    bad = session_record(
        trigger="owner-message", approvals=[{"tool": "spy", "decision": "approved"}]
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(bad)
