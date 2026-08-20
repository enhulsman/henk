"""Delivery's audit receipts (reminder-delivery group 6), from the audit-log contract.

The scheduler is exercised through the **real** `AuditLog` and `ReminderReceipts` here,
not through a collecting double: the claim is that a durable JSONL record exists on disk
and validates against the committed v4 document, and a double proves neither half.

The whole point of this group is that it needs no schema version bump. `reminders-core`
declared v4's complete transition enumeration and its `scheduler` initiator precisely so
the delivery half could ship without one — a schema document being a validation contract
rather than an inventory of what the current build happens to emit. Every test below
asserts against `SCHEMA_VERSION == 4` and the same document that shipped then.
"""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

import jsonschema
import pytest

from henk.audit import (
    AUDIT_SCHEMA_PATH,
    REMINDER_TRANSITIONS,
    SCHEMA_VERSION,
    AuditLog,
    ReminderReceipts,
)
from henk.channel.base import SendOutcome
from henk.config import RemindersConfig
from henk.reminders.scheduler import ReminderScheduler
from henk.reminders.timeparse import TimeResolver
from henk.store import Store
from henk.store.reminders import (
    ABANDONED,
    DELIVERED,
    DELIVERED_LATE,
    MISSED,
    ReminderStore,
)
from tests.test_reminders_scheduler import (
    CRASH_LIMIT,
    GRACE,
    NOW,
    THRESHOLD,
    TZ,
    Clock,
    OutcomeChannel,
)

SCHEMA = json.loads(AUDIT_SCHEMA_PATH.read_text())

#: The four transitions only the delivery half writes.
DELIVERY_TRANSITIONS = (DELIVERED, DELIVERED_LATE, MISSED, ABANDONED)


def _build(tmp_path: Path, *, channel=None, clock=None):
    """A scheduler wired to a real AuditLog on disk."""
    clock = clock or Clock()
    channel = channel if channel is not None else OutcomeChannel()
    store = Store(tmp_path / "store" / "henk.db", clock=clock)
    repo = ReminderStore(store)
    audit_path = tmp_path / "audit" / "henk-audit.jsonl"
    scheduler = ReminderScheduler(
        repo,
        channel,
        config=RemindersConfig(
            enabled=True,
            late_grace_seconds=GRACE,
            late_delivery_threshold_seconds=THRESHOLD,
            crash_attempt_limit=CRASH_LIMIT,
        ),
        resolver=TimeResolver(ZoneInfo(TZ), clock=clock),
        receipts=ReminderReceipts(AuditLog(audit_path, clock=clock)),
        clock=clock,
        sleep=_yield,
    )
    return store, repo, scheduler, channel, audit_path


async def _yield(_):
    return None


def _seed(repo, *, due_at, text="call the plumber"):
    return repo.schedule(text, due_at=due_at, due_tz=TZ, input_spec="+1h")


def _records(audit_path: Path) -> list[dict]:
    """Every reminder record on disk, parsed. Read back, never remembered."""
    if not audit_path.exists():
        return []
    return [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("record_type") == "reminder"
    ]


# --- 6.1 Every delivery transition is receipted -------------------------


@pytest.mark.parametrize("transition", DELIVERY_TRANSITIONS)
async def test_each_delivery_transition_writes_one_validating_record(
    tmp_path: Path, transition: str
):
    path = tmp_path / transition
    store, repo, scheduler, channel, audit_path = _build(path)
    if transition == DELIVERED:
        row = _seed(repo, due_at=NOW - 60)
    elif transition == DELIVERED_LATE:
        row = _seed(repo, due_at=NOW - THRESHOLD - 60)
    elif transition == MISSED:
        row = _seed(repo, due_at=NOW - GRACE - 60)
    else:  # ABANDONED
        row = _seed(repo, due_at=NOW - 60)
        for _ in range(CRASH_LIMIT - 1):
            repo.charge_attempt(row.id)

    await scheduler.tick()

    records = [r for r in _records(audit_path) if r["transition"] == transition]
    assert len(records) == 1, _records(audit_path)
    record = records[0]
    jsonschema.validate(record, SCHEMA)
    assert record["schema_version"] == 4
    assert record["record_type"] == "reminder"
    assert record["reminder_id"] == row.id
    assert record["due_at"] == row.due_at
    # The instant, not a rendered string: rendering depends on the currently
    # configured zone, and a receipt must not move when the config does.
    assert isinstance(record["due_at"], float)
    assert record["initiated_by"] == "scheduler"
    assert record["at"] == NOW
    store.close()


async def test_no_delivery_record_carries_the_reminders_text(tmp_path: Path):
    """The store holds the content; the log holds the evidence.

    Driven with text that would be unmistakable if it leaked, and checked against the
    raw file rather than the parsed record — a nested or renamed field would still
    show up in the bytes.
    """
    secret = "collect the spare key from Marieke at number 14"
    store, repo, scheduler, channel, audit_path = _build(tmp_path)
    _seed(repo, due_at=NOW - GRACE - 60, text=secret)
    _seed(repo, due_at=NOW - 60, text=secret)
    await scheduler.tick()
    blob = audit_path.read_text(encoding="utf-8")
    assert secret not in blob
    assert "Marieke" not in blob
    for record in _records(audit_path):
        for key in ("text", "reminder_text", "content"):
            assert key not in record
    store.close()


async def test_the_schema_version_is_not_bumped_by_the_delivery_half(tmp_path: Path):
    """v4 was written to make this group a no-op for the version pin."""
    assert SCHEMA_VERSION == 4
    assert SCHEMA["properties"]["schema_version"]["const"] == 4
    # And every transition the scheduler can write was already enumerated by v4.
    for transition in DELIVERY_TRANSITIONS:
        assert transition in REMINDER_TRANSITIONS
    assert "scheduler" in SCHEMA["properties"]["initiated_by"]["enum"]

    store, repo, scheduler, channel, audit_path = _build(tmp_path)
    _seed(repo, due_at=NOW - 60)
    await scheduler.tick()
    for record in _records(audit_path):
        assert record["schema_version"] == 4
    store.close()


async def test_a_partial_delivery_records_detail_partial(tmp_path: Path):
    """The degraded delivery is durable, not merely a log line.

    A `partial` reminder send is recorded as delivered — the head that landed IS the
    reminder — so without this the record would be indistinguishable from a clean
    delivery, and the one durable trace of the degradation would be a log line that
    rotates away.
    """
    channel = OutcomeChannel()
    channel.default = SendOutcome.PARTIAL
    store, repo, scheduler, _, audit_path = _build(tmp_path, channel=channel)
    row = _seed(repo, due_at=NOW - 60)
    await scheduler.tick()
    records = _records(audit_path)
    assert len(records) == 1
    assert records[0]["transition"] == DELIVERED
    assert records[0]["detail"] == "partial"
    jsonschema.validate(records[0], SCHEMA)
    store.close()


async def test_a_clean_delivery_records_no_detail(tmp_path: Path):
    store, repo, scheduler, channel, audit_path = _build(tmp_path)
    _seed(repo, due_at=NOW - 60)
    await scheduler.tick()
    records = _records(audit_path)
    assert records[0]["detail"] is None
    jsonschema.validate(records[0], SCHEMA)
    store.close()


async def test_a_failed_send_writes_no_transition_record(tmp_path: Path):
    """Receipts record state changes, and a failed send is not one.

    The row stays pending on the floor. A record here would claim a transition the
    store did not make, which is the one direction the audit log must never fail in.
    """
    channel = OutcomeChannel()
    channel.default = SendOutcome.FAILED
    store, repo, scheduler, _, audit_path = _build(tmp_path, channel=channel)
    _seed(repo, due_at=NOW - 60)
    await scheduler.tick()
    assert _records(audit_path) == []
    store.close()


async def test_a_summary_that_is_not_delivered_writes_no_report_record(
    tmp_path: Path,
):
    """And the give-up exits write none either — deliberately.

    `reported_at` written as a give-up is not a transition and not a claim the owner
    was told; it is Henk stopping. The evidence for it is an error log, and putting it
    in the audit trail would make the trail assert something false.
    """
    channel = OutcomeChannel()
    channel.default = SendOutcome.FAILED
    store, repo, scheduler, _, audit_path = _build(tmp_path, channel=channel)
    _seed(repo, due_at=NOW - GRACE - 60)
    await scheduler.tick()
    # Exactly one record: the `missed` transition, which DID happen in the store.
    transitions = [r["transition"] for r in _records(audit_path)]
    assert transitions == [MISSED]
    store.close()


async def test_the_record_is_on_disk_before_the_next_transition_can_run(
    tmp_path: Path,
):
    """Appended at the transition, so a crash after it finds the record intact.

    Death is simulated by closing the store after the tick and reopening the file —
    the record has to be found by a reader that shares nothing with the process that
    wrote it.
    """
    store, repo, scheduler, channel, audit_path = _build(tmp_path)
    row = _seed(repo, due_at=NOW - 60)
    await scheduler.tick()
    store.close()  # the process ends here

    reopened = Store(tmp_path / "store" / "henk.db")
    try:
        assert ReminderStore(reopened).get(row.id).status == DELIVERED
    finally:
        reopened.close()
    records = _records(audit_path)
    assert [r["transition"] for r in records] == [DELIVERED]
    assert records[0]["reminder_id"] == row.id


async def test_a_delivered_reminders_trail_carries_both_records(tmp_path: Path):
    """The traceability scenario from the approval-gate delta.

    A `scheduled` record naming who asked for it, and a delivery record naming the
    scheduler. Two records for two questions — who authorized this, and what happened
    — which is why they are separate rather than one record with a mutable field.
    """
    clock = Clock()
    store, repo, scheduler, channel, audit_path = _build(tmp_path, clock=clock)
    # The scheduling receipt is written by the tool/command layer; here it is written
    # directly, because this test is about the TRAIL, not about who writes each half.
    scheduling = ReminderReceipts(AuditLog(audit_path, clock=clock))
    row = _seed(repo, due_at=NOW - 60)
    scheduling.record(
        reminder_id=row.id,
        due_at=row.due_at,
        transition="scheduled",
        initiated_by="owner-command",
    )
    await scheduler.tick()

    trail = [r for r in _records(audit_path) if r["reminder_id"] == row.id]
    assert [r["transition"] for r in trail] == ["scheduled", DELIVERED]
    assert trail[0]["initiated_by"] == "owner-command"
    assert trail[1]["initiated_by"] == "scheduler"
    for record in trail:
        jsonschema.validate(record, SCHEMA)
    store.close()


async def test_a_cancelled_then_delivered_row_carries_both_transitions(
    tmp_path: Path,
):
    """The race the design records honestly rather than closing.

    A cancellation that commits after dispatch may still deliver. The row records
    `delivered`, because the message factually reached the owner — and both
    transitions are in the trail, which is what makes the sequence reconstructible
    afterwards instead of looking like a contradiction.
    """
    clock = Clock()
    store, repo, scheduler, channel, audit_path = _build(tmp_path, clock=clock)
    receipts = ReminderReceipts(AuditLog(audit_path, clock=clock))
    row = _seed(repo, due_at=NOW - THRESHOLD - 60)
    # The pre-work selection happens, then the cancellation commits, then the send
    # completes — modelled here by recording the cancellation between the two.
    plan = scheduler._pre_work(NOW)
    assert [r.id for r in plan.deliveries] == [row.id]
    receipts.record(
        reminder_id=row.id,
        due_at=row.due_at,
        transition="cancelled",
        initiated_by="owner-command",
    )
    # The row is still pending in the store (the cancellation above is only its
    # receipt), so the send goes out and the post-send write records the truth.
    await scheduler._deliver(plan.deliveries[0], NOW)

    trail = [r["transition"] for r in _records(audit_path)]
    assert trail == ["cancelled", DELIVERED_LATE]
    assert repo.get(row.id).status == DELIVERED_LATE
    store.close()


async def test_every_record_the_scheduler_writes_names_the_scheduler(tmp_path: Path):
    """Never `model`, never `owner-command`: a delivery is app-initiated.

    Which is the audit half of "delivery is outside the approval gate" — the record is
    where accountability for a delivery lives, since no approval was involved.
    """
    store, repo, scheduler, channel, audit_path = _build(tmp_path)
    _seed(repo, due_at=NOW - 60, text="on time")
    _seed(repo, due_at=NOW - THRESHOLD - 60, text="late")
    _seed(repo, due_at=NOW - GRACE - 60, text="missed")
    crashed = _seed(repo, due_at=NOW - 120, text="abandoned")
    for _ in range(CRASH_LIMIT - 1):
        repo.charge_attempt(crashed.id)
    await scheduler.tick()

    records = _records(audit_path)
    assert len(records) == 4, [r["transition"] for r in records]
    assert {r["initiated_by"] for r in records} == {"scheduler"}
    assert {r["transition"] for r in records} == {
        DELIVERED,
        DELIVERED_LATE,
        MISSED,
        ABANDONED,
    }
    for record in records:
        jsonschema.validate(record, SCHEMA)
    store.close()


async def test_an_audit_write_failure_does_not_stop_a_delivery(tmp_path: Path):
    """Loud but non-blocking, exactly like every other audit write.

    A receipt that could take the delivery path down would make the log a liability
    rather than evidence — the owner's reminder matters more than its paperwork.
    """
    store, repo, scheduler, channel, audit_path = _build(tmp_path)
    # Point the log at a path that cannot be created.
    scheduler._receipts = ReminderReceipts(AuditLog(tmp_path / "store" / "henk.db" / "nope.jsonl"))
    row = _seed(repo, due_at=NOW - 60)
    await scheduler.tick()
    assert repo.get(row.id).status == DELIVERED
    assert len(channel.sent) == 1
    store.close()
