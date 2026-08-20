"""Audit schema v4 and the reminder lifecycle record (group 5).

From the audit-log spec's "Reminder lifecycle records are durable at each
transition", "A mutating reminder tool call writes two records for two questions",
and the modified "Schema is versioned".

The two-records rule is the thing to keep straight: an `authorization` record answers
*was the agent allowed to do this?* and is written when the gate decides; a
`reminder` record answers *what changed?* and is written after the store commits.
They are not collapsed, because one existing without the other is itself evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from henk.audit import (
    AUDIT_SCHEMA_PATH,
    AUDIT_SCHEMA_V1_PATH,
    AUDIT_SCHEMA_V2_PATH,
    AUDIT_SCHEMA_V3_PATH,
    AUDIT_SCHEMA_V4_PATH,
    REMINDER_INITIATORS,
    REMINDER_TRANSITIONS,
    SCHEMA_VERSION,
    AuditLog,
    ReminderReceipts,
    authorization_record,
    reminder_record,
    session_record,
)

SCHEMA = json.loads(AUDIT_SCHEMA_PATH.read_text())


def _validate(record) -> None:
    jsonschema.validate(record, SCHEMA)


# --- The version and the documents ---------------------------------------


def test_the_current_version_is_four_and_its_document_is_committed():
    assert SCHEMA_VERSION == 4
    assert AUDIT_SCHEMA_V4_PATH == AUDIT_SCHEMA_PATH
    assert AUDIT_SCHEMA_PATH.name == "audit-record.v4.schema.json"
    assert SCHEMA["properties"]["schema_version"]["const"] == 4


def test_every_prior_version_document_stays_committed():
    # "Schema is versioned" obliges every prior version's document to remain, so a
    # historical record still validates against the version it declares.
    for path, version in (
        (AUDIT_SCHEMA_V1_PATH, 1),
        (AUDIT_SCHEMA_V2_PATH, 2),
        (AUDIT_SCHEMA_V3_PATH, 3),
    ):
        assert path.exists(), path
        assert json.loads(path.read_text())["properties"]["schema_version"][
            "const"
        ] == version


def test_new_records_of_every_type_declare_version_four():
    for record in (
        session_record(trigger="owner-message"),
        reminder_record(reminder_id=1, due_at=1.0, transition="scheduled"),
    ):
        assert record["schema_version"] == 4
        _validate(record)


# --- The reminder record's shape -----------------------------------------


def test_a_reminder_record_carries_id_due_time_transition_initiator_and_timestamp():
    record = reminder_record(
        reminder_id=12,
        due_at=1787635800.0,
        transition="scheduled",
        initiated_by="model",
    )
    _validate(record)
    assert record["record_type"] == "reminder"
    assert record["reminder_id"] == 12
    assert record["due_at"] == 1787635800.0
    assert record["transition"] == "scheduled"
    assert record["initiated_by"] == "model"
    assert "at" in record  # stamped by the writer


def test_a_reminder_record_carries_no_reminder_text():
    # The store holds the content; the log holds the evidence, and the log gets read
    # and pasted around in contexts where owner-personal free text does not belong.
    record = reminder_record(reminder_id=1, due_at=1.0, transition="scheduled")
    blob = json.dumps(record)
    for key in ("text", "reminder_text", "content"):
        assert key not in record
    assert "buy bread" not in blob  # nothing resembling a payload is carried
    # `detail` IS present now (reminder-delivery needs to mark a partial delivery
    # durably), and it is null unless a caller sets it. It used to be asserted absent
    # here, for a reason that still holds — a free-text property on a reminder record
    # is a route for owner-personal text into the log — so rather than dropping that
    # guarantee, the contract now CONSTRAINS the property: see the enum test below.
    assert record["detail"] is None


def test_a_reminder_records_detail_is_a_closed_vocabulary_not_free_text():
    """The property that replaces "reminder records have no `detail`".

    `detail` is free text on an authorization record, where the writer is naming which
    memory was removed. On a reminder record it must never be free text, or it becomes
    the one field through which the reminder's own wording could reach the log — which
    is exactly what the no-text guarantee exists to prevent. So the reminder branch of
    the document pins it to an enum, and this asserts the document does the refusing
    rather than the builder.
    """
    partial = reminder_record(
        reminder_id=1, due_at=1.0, transition="delivered", detail="partial"
    )
    _validate(partial)
    assert partial["detail"] == "partial"

    for smuggled in ("buy bread", "partial: buy bread", "PARTIAL", ""):
        record = reminder_record(
            reminder_id=1, due_at=1.0, transition="delivered", detail=smuggled
        )
        with pytest.raises(jsonschema.ValidationError):
            _validate(record)

    # Tightening the reminder branch is deliberately NOT a version bump: no reminder
    # record with a `detail` value has ever been written, so nothing already on disk
    # becomes invalid, and an authorization record's free-text `detail` is untouched.
    assert SCHEMA_VERSION == 4
    authorization = authorization_record(
        tool="capture",
        tier="standing",
        outcome="authorized",
        detail="removed 3 memories",
    )
    _validate(authorization)
    assert authorization["detail"] == "removed 3 memories"


def test_the_document_itself_refuses_a_record_that_smuggles_text_in():
    # Enforced by the committed contract, not only by the builder — a future writer
    # that hand-builds a record cannot slip the text past validation.
    for key in ("text", "reminder_text", "content"):
        smuggled = reminder_record(reminder_id=1, due_at=1.0, transition="scheduled")
        smuggled[key] = "buy bread"
        with pytest.raises(jsonschema.ValidationError):
            _validate(smuggled)


def test_the_due_time_is_the_instant_not_a_rendered_string():
    # Rendering depends on the currently configured zone; a receipt must not move
    # when the config does.
    record = reminder_record(reminder_id=1, due_at=1787635800.0, transition="scheduled")
    assert isinstance(record["due_at"], float)
    _validate(record)


@pytest.mark.parametrize(
    "missing", ["reminder_id", "transition", "initiated_by", "due_at"]
)
def test_the_document_requires_every_reminder_field(missing):
    record = reminder_record(reminder_id=1, due_at=1.0, transition="scheduled")
    del record[missing]
    with pytest.raises(jsonschema.ValidationError):
        _validate(record)


# --- The COMPLETE transition enumeration (delivery's half included) ------


def test_the_complete_transition_enumeration_validates_before_delivery_exists():
    # v4 ships the whole vocabulary so `reminder-delivery` needs no version bump: a
    # schema document is a validation contract, not an inventory of what the current
    # build emits.
    assert set(REMINDER_TRANSITIONS) == {
        "scheduled",
        "cancelled",
        "reinstated",
        "delivered",
        "delivered-late",
        "missed",
        "abandoned",
    }
    assert set(SCHEMA["properties"]["transition"]["enum"]) == set(
        REMINDER_TRANSITIONS
    )
    for transition in REMINDER_TRANSITIONS:
        _validate(
            reminder_record(
                reminder_id=1,
                due_at=1.0,
                transition=transition,
                initiated_by="scheduler",
            )
        )


def test_the_scheduler_initiator_validates_before_the_scheduler_exists():
    assert set(REMINDER_INITIATORS) == {"model", "owner-command", "scheduler"}
    assert set(SCHEMA["properties"]["initiated_by"]["enum"]) == set(
        REMINDER_INITIATORS
    )
    for initiator in REMINDER_INITIATORS:
        _validate(
            reminder_record(
                reminder_id=1, due_at=1.0, transition="delivered",
                initiated_by=initiator,
            )
        )


def test_an_invented_transition_is_refused():
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            reminder_record(reminder_id=1, due_at=1.0, transition="snoozed")
        )


def test_reinstated_is_a_transition_name_not_a_row_status():
    # A reinstated reminder's stored row status is `pending`; the record names what
    # happened, not what the row now says.
    from henk.store.reminders import STATUSES

    assert "reinstated" in REMINDER_TRANSITIONS
    assert "reinstated" not in STATUSES


# --- Durability at transition time ---------------------------------------


def test_a_reminder_record_is_on_disk_before_a_sigkill_could_intervene(tmp_path: Path):
    # Written the moment the transition happens, not at session close — an
    # interpreter that dies immediately afterwards leaves it behind.
    log = tmp_path / "audit.jsonl"
    ReminderReceipts(AuditLog(log)).record(
        reminder_id=7, due_at=1787635800.0, transition="scheduled",
        initiated_by="owner-command",
    )
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["record_type"] == "reminder"
    assert lines[0]["reminder_id"] == 7
    assert lines[0]["initiated_by"] == "owner-command"
    assert lines[0]["at"] is not None  # stamped by the writer, not left null
    _validate(lines[0])


def test_a_reminder_receipt_write_failure_is_loud_but_never_blocking(tmp_path: Path):
    # Same contract as every other audit write: the caller continues regardless.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    receipts = ReminderReceipts(AuditLog(blocker / "audit.jsonl"))
    assert receipts.record(reminder_id=1, due_at=1.0, transition="scheduled")


def test_no_audit_configured_is_a_designed_no_op(tmp_path: Path):
    assert ReminderReceipts(None).record(
        reminder_id=1, due_at=1.0, transition="scheduled"
    )


# --- v3 records still validate against v3's document ---------------------


def test_a_v3_record_validates_against_v3_and_not_against_v4():
    v3_document = json.loads(AUDIT_SCHEMA_V3_PATH.read_text())
    v3_record = {
        "schema_version": 3,
        "record_type": "authorization",
        "tool": "capture",
        "tier": "standing",
        "outcome": "authorized",
        "initiated_by": "model",
        "at": 3.0,
    }
    jsonschema.validate(v3_record, v3_document)
    # And v4's document pins its own version, so the two cannot be confused.
    with pytest.raises(jsonschema.ValidationError):
        _validate(v3_record)
    # A `reminder` record is not expressible in v3 at all, which is why the version
    # had to increment.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            reminder_record(reminder_id=1, due_at=1.0, transition="scheduled"),
            v3_document,
        )
