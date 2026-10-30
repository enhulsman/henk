"""Audit schema v6: the `calendar`, `hold` and `hold-release` records (maintenance-calendar
group 4).

From the audit-log delta: *Schema is versioned* (version 6, old documents kept, a
mixed log rehydrates identically, hold records do not disturb rehydration), *A
calendar write is receipted twice, without its title*, and *Holds and releases are
recorded without payloads*; design D5.

What this group can and cannot show. The builders, the writers and the document are
here. The behaviours that rest on durable rows belong to the code that owns those
rows: "no second `hold` on a duplicate" is the `INSERT OR IGNORE` on `held_events`
and "one `hold-release` per window" is the `released_at IS NULL` guard, both group
6; "a calendar record only after a confirmed insert" is the create tool, group 3.
What is tested here is the contract those call sites use: a writer that reports
whether it wrote, so the caller decides, and a document that refuses any title,
message or payload slot outright.

Every fixture uses placeholder identities and ids; no live audit data.
"""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest

from henk.audit import (
    AUDIT_SCHEMA_PATH,
    AUDIT_SCHEMA_V5_PATH,
    AUDIT_SCHEMA_V6_PATH,
    SCHEMA_VERSION,
    AuditLog,
    CalendarReceipts,
    HoldReceipts,
    authorization_record,
    calendar_record,
    hold_record,
    hold_release_record,
    reminder_record,
    session_record,
    suppression_record,
)
from henk.events.pipeline import EventPipeline, PipelineConfig

SCHEMA = json.loads(AUDIT_SCHEMA_PATH.read_text())
V5 = json.loads(AUDIT_SCHEMA_V5_PATH.read_text())

NEW_TYPES = ("calendar", "hold", "hold-release")
TITLE = "Homelab: patch round"
MESSAGE = "svc/example-a is DOWN: connection refused on 10.0.0.1"
RAW = '{"id":"ev-1","title":"Gatus: svc/example-a","message":"..."}'


def _validate(record) -> None:
    jsonschema.validate(record, SCHEMA)


def _calendar(**kw):
    base = dict(
        event_id="evt-placeholder-1",
        start="2026-11-07T20:00:00+01:00",
        end="2026-11-07T22:00:00+01:00",
        reference="ref-example-1",
    )
    base.update(kw)
    return calendar_record(**base)


def _hold(**kw):
    base = dict(
        identity_key="gatus:svc/example-a",
        state="firing",
        event_id="ntfy-example-1",
        window_id="evt-placeholder-1",
        window_end=1_794_085_200.0,
        published_at=1_794_078_000.0,
    )
    base.update(kw)
    return hold_record(**base)


def _release(**kw):
    base = dict(window_id="evt-placeholder-1", held=4, recovered=3, forwarded=1)
    base.update(kw)
    return hold_release_record(**base)


# --- Version 6 -------------------------------------------------------------


def test_the_current_version_is_six_and_its_document_is_committed():
    assert SCHEMA_VERSION == 6
    assert AUDIT_SCHEMA_V6_PATH == AUDIT_SCHEMA_PATH
    assert AUDIT_SCHEMA_PATH.name == "audit-record.v6.schema.json"
    assert SCHEMA["properties"]["schema_version"]["const"] == 6
    assert set(NEW_TYPES) <= set(SCHEMA["properties"]["record_type"]["enum"])


def test_new_records_declare_the_new_version_and_validate():
    # Scenario "New records declare the new version": every builder stamps v6.
    for record in (
        session_record(trigger="owner-message"),
        suppression_record(identity_key="gatus:svc/example-a", reason="cooldown"),
        authorization_record(tool="calendar_create_event", outcome="approved",
                             tier="per-instance", reference="ref-example-1",
                             turn_type="owner"),
        reminder_record(reminder_id=1, due_at=1.0, transition="scheduled"),
        _calendar(),
        _hold(),
        _release(),
    ):
        assert record["schema_version"] == 6
        _validate(record)


def test_the_v5_document_stays_committed_and_refuses_v6():
    # Scenario "Old records remain valid": the v5 document is the one that shipped.
    assert AUDIT_SCHEMA_V5_PATH.exists()
    assert AUDIT_SCHEMA_V5_PATH != AUDIT_SCHEMA_PATH
    assert V5["properties"]["schema_version"]["const"] == 5
    for new_type in NEW_TYPES:
        assert new_type not in V5["properties"]["record_type"]["enum"]
    v5_record = session_record(trigger="owner-message") | {"schema_version": 5}
    jsonschema.validate(v5_record, V5)
    with pytest.raises(jsonschema.ValidationError):
        _validate(v5_record)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_hold(), V5)


def test_no_v5_field_or_branch_is_changed_except_the_transition_union():
    # "Version 6 SHALL NOT rename, retype, or change the meaning of any version-5
    # field or record type." Two v5 nodes change shape, and both only widen to admit
    # the new types while the old types keep exactly their v5 constraint:
    #   - `record_type` gains the three new names;
    #   - `transition` gains `created` at the top level, and the reminder branch
    #     re-pins the reminder records to v5's seven values (asserted below).
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "description"}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    v5_props, v6_props = strip(V5["properties"]), strip(SCHEMA["properties"])
    for name, spec in v5_props.items():
        if name in ("schema_version", "record_type", "transition"):
            continue
        assert v6_props[name] == spec, name
    assert v6_props["record_type"]["enum"] == V5["properties"]["record_type"]["enum"] + list(
        NEW_TYPES
    )
    assert v6_props["transition"]["enum"] == V5["properties"]["transition"]["enum"] + ["created"]
    v5_branches = strip(V5["allOf"])
    v6_branches = strip(SCHEMA["allOf"])
    # Every v5 branch is carried over in order; only the reminder branch differs,
    # by the re-pin of `transition` to exactly v5's reminder values.
    assert len(v6_branches) == len(v5_branches) + len(NEW_TYPES)
    for branch, original in zip(v6_branches, v5_branches):
        if original["if"]["properties"]["record_type"]["const"] == "reminder":
            branch = copy.deepcopy(branch)
            assert branch["then"]["properties"].pop("transition") == {
                "enum": V5["properties"]["transition"]["enum"]
            }
        assert branch == original
    assert SCHEMA.get("required") == V5.get("required")
    assert SCHEMA.get("additionalProperties") == V5.get("additionalProperties")
    new_props = set(v6_props) - set(v5_props)
    assert new_props == {
        "window_id", "window_end", "published_at", "state",
        "held", "recovered", "forwarded", "start", "end",
    }


def test_a_reminder_record_still_cannot_carry_created():
    record = reminder_record(reminder_id=1, due_at=1.0, transition="scheduled")
    record["transition"] = "created"
    with pytest.raises(jsonschema.ValidationError):
        _validate(record)


@pytest.mark.parametrize(
    "transition", ["scheduled", "cancelled", "reinstated", "delivered",
                   "delivered-late", "missed", "abandoned"]
)
def test_every_v5_reminder_transition_still_validates(transition):
    _validate(reminder_record(reminder_id=1, due_at=1.0, transition=transition))


# --- calendar --------------------------------------------------------------


def test_calendar_record_shape():
    record = _calendar()
    assert record == {
        "schema_version": 6,
        "record_type": "calendar",
        "transition": "created",
        "event_id": "evt-placeholder-1",
        "start": "2026-11-07T20:00:00+01:00",
        "end": "2026-11-07T22:00:00+01:00",
        "initiated_by": "model",
        "reference": "ref-example-1",
        "at": None,
    }
    _validate(record)


def test_calendar_record_has_no_title_parameter():
    # Scenario "Created event receipted": the record does not contain the title.
    # The builder cannot take one, so no call site can pass it by accident.
    import inspect

    params = set(inspect.signature(calendar_record).parameters)
    assert not params & {"title", "summary", "text", "description"}
    assert TITLE not in json.dumps(_calendar())


@pytest.mark.parametrize("slot", ["title", "summary", "description", "message", "text", "attendees"])
def test_the_document_refuses_a_title_or_payload_on_a_calendar_record(slot):
    record = _calendar()
    record[slot] = TITLE
    with pytest.raises(jsonschema.ValidationError):
        _validate(record)


@pytest.mark.parametrize(
    "field, bad",
    [
        ("transition", "deleted"),
        ("transition", "scheduled"),
        ("initiated_by", "owner-command"),
        ("initiated_by", "scheduler"),
        ("event_id", None),
        ("event_id", ""),
        ("reference", None),
        ("start", "Sat 2026-11-07 20:00"),
        ("start", "2026-11-07T20:00:00"),  # no offset
        ("end", 1794085200.0),
    ],
)
def test_calendar_record_fields_are_typed(field, bad):
    record = _calendar()
    record[field] = bad
    with pytest.raises(jsonschema.ValidationError):
        _validate(record)


@pytest.mark.parametrize("field", ["transition", "event_id", "start", "end", "initiated_by", "reference"])
def test_calendar_record_requires_every_field(field):
    record = _calendar()
    del record[field]
    with pytest.raises(jsonschema.ValidationError):
        _validate(record)


def test_calendar_utc_instants_validate():
    _validate(_calendar(start="2026-11-07T19:00:00Z", end="2026-11-07T21:00:00Z"))


def test_calendar_receipts_write_one_record_and_report_success(tmp_path):
    # The writer the create tool calls after a confirmed insert (group 3 wires it).
    path = tmp_path / "audit.jsonl"
    receipts = CalendarReceipts(AuditLog(path, clock=lambda: 42.0))
    assert receipts.created(
        event_id="evt-placeholder-1",
        start="2026-11-07T20:00:00+01:00",
        end="2026-11-07T22:00:00+01:00",
        reference="ref-example-1",
    ) is True
    [line] = path.read_text().splitlines()
    record = json.loads(line)
    assert record["record_type"] == "calendar"
    assert record["at"] == 42.0
    _validate(record)


def test_calendar_receipts_without_audit_write_nothing():
    assert CalendarReceipts(None).created(
        event_id="e", start="2026-11-07T20:00:00+01:00",
        end="2026-11-07T22:00:00+01:00", reference="r",
    ) is True


def test_a_failed_calendar_write_is_reported_not_raised(tmp_path, caplog):
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("")
    receipts = CalendarReceipts(AuditLog(blocked / "audit.jsonl"))
    assert receipts.created(
        event_id="e", start="2026-11-07T20:00:00+01:00",
        end="2026-11-07T22:00:00+01:00", reference="r",
    ) is False
    assert "audit write failed" in caplog.text


# --- hold and hold-release ---------------------------------------------------


def test_hold_record_shape():
    record = _hold()
    assert record == {
        "schema_version": 6,
        "record_type": "hold",
        "identity_key": "gatus:svc/example-a",
        "state": "firing",
        "event_id": "ntfy-example-1",
        "window_id": "evt-placeholder-1",
        "window_end": 1_794_085_200.0,
        "published_at": 1_794_078_000.0,
        "at": None,
    }
    _validate(record)


def test_hold_record_carries_no_payload():
    # Scenario "Hold record carries no payload": the identity key and the window id
    # are there; the message text M is not, and the builder cannot take it.
    import inspect

    record = _hold()
    assert record["identity_key"] and record["window_id"]
    text = json.dumps(record)
    for payload in (MESSAGE, TITLE, RAW):
        assert payload not in text
    params = set(inspect.signature(hold_record).parameters)
    assert not params & {"title", "message", "raw", "raw_json", "window_title", "text"}


@pytest.mark.parametrize(
    "slot", ["title", "message", "raw", "raw_json", "window_title", "summary", "text", "content"]
)
def test_the_document_refuses_a_payload_on_hold_records(slot):
    for record in (_hold(), _release()):
        record[slot] = MESSAGE
        with pytest.raises(jsonschema.ValidationError):
            _validate(record)


@pytest.mark.parametrize(
    "field", ["identity_key", "state", "event_id", "window_id", "window_end", "published_at"]
)
def test_hold_record_requires_every_field(field):
    record = _hold()
    del record[field]
    with pytest.raises(jsonschema.ValidationError):
        _validate(record)


@pytest.mark.parametrize(
    "field, bad",
    [("window_end", "22:30"), ("published_at", None), ("window_id", 7), ("state", None)],
)
def test_hold_record_fields_are_typed(field, bad):
    record = _hold()
    record[field] = bad
    with pytest.raises(jsonschema.ValidationError):
        _validate(record)


def test_release_counts_recorded():
    # Scenario "Release counts recorded": 4 held, 3 recovered → forwarded 1.
    record = _release()
    assert (record["held"], record["recovered"], record["forwarded"]) == (4, 3, 1)
    assert record == {
        "schema_version": 6,
        "record_type": "hold-release",
        "window_id": "evt-placeholder-1",
        "held": 4,
        "recovered": 3,
        "forwarded": 1,
        "at": None,
    }
    _validate(record)


def test_release_counts_must_add_up():
    # Counts are distinct identities: each held identity is either recovered or
    # forwarded, never both and never neither.
    for held, recovered, forwarded in ((4, 3, 2), (4, 5, 0), (1, 0, 0), (-1, 0, -1)):
        with pytest.raises(ValueError):
            _release(held=held, recovered=recovered, forwarded=forwarded)


def test_an_all_recovered_release_validates():
    _validate(_release(held=1, recovered=1, forwarded=0))


@pytest.mark.parametrize("field", ["window_id", "held", "recovered", "forwarded"])
def test_release_record_requires_every_field(field):
    record = _release()
    del record[field]
    with pytest.raises(jsonschema.ValidationError):
        _validate(record)


@pytest.mark.parametrize("field, bad", [("held", -1), ("recovered", 1.5), ("forwarded", "1")])
def test_release_counts_are_non_negative_integers(field, bad):
    record = _release()
    record[field] = bad
    with pytest.raises(jsonschema.ValidationError):
        _validate(record)


def test_hold_receipts_write_and_report(tmp_path):
    path = tmp_path / "audit.jsonl"
    receipts = HoldReceipts(AuditLog(path, clock=lambda: 7.0))
    assert receipts.held(
        identity_key="gatus:svc/example-a", state="firing", event_id="ntfy-example-1",
        window_id="w", window_end=2.0, published_at=1.0,
    ) is True
    assert receipts.released(window_id="w", held=1, recovered=1, forwarded=0) is True
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["record_type"] for r in records] == ["hold", "hold-release"]
    for record in records:
        _validate(record)


def test_a_failed_hold_write_is_reported_not_raised(tmp_path):
    # The hold stands when only its record fails (event-intake spec): the writer
    # must return, not raise, so the caller can log loud and keep the row.
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("")
    receipts = HoldReceipts(AuditLog(blocked / "audit.jsonl"))
    assert receipts.held(
        identity_key="k", state="firing", event_id="e", window_id="w",
        window_end=2.0, published_at=1.0,
    ) is False


# --- Rehydration is unchanged by hold records -------------------------------


HOUR = 3600.0
EPOCH = 1_700_000_000.0
NOW = EPOCH + 8.5 * HOUR


def _pipeline() -> EventPipeline:
    return EventPipeline(
        PipelineConfig(
            debounce_seconds=120.0,
            cooldown_seconds=6 * HOUR,
            recurrence_window_seconds=24 * HOUR,
            cap_per_24h=3,
            cooldown_overrides=[],
        )
    )


def _state(pipe: EventPipeline) -> dict:
    return {
        "cooldown": dict(pipe._last_triaged),
        "recurrence": dict(pipe._last_handoff_ref),
        "cap_times": list(pipe._announce_times),
        "cap_suppressed": pipe._cap_suppressed_since_announce,
    }


def _triage(identity, at, *, announceable, handoff=None):
    return session_record(
        trigger="event", event=[{"identity_key": identity}],
        handoff_message_id=handoff, announceable=announceable, at=at,
    )


def test_hold_records_do_not_disturb_rehydration():
    # Scenario "Hold records do not disturb rehydration". The hold records name the
    # SAME identities as the triages and carry `identity_key`, `event_id` and an
    # `at` inside every window, so a reader that keyed on those would be fooled.
    triages = [
        _triage("gatus:svc/example-a", EPOCH, announceable=True, handoff="hf-a"),
        _triage("gatus:svc/example-b", EPOCH + HOUR, announceable=False),
        _triage("gatus:svc/example-c", EPOCH + 2 * HOUR, announceable=True),
    ]
    holds = [
        _hold(identity_key="gatus:svc/example-a", event_id="h1") | {"at": EPOCH + 3 * HOUR},
        _hold(identity_key="gatus:svc/example-d", event_id="h2") | {"at": EPOCH + 4 * HOUR},
        _release(held=2, recovered=1, forwarded=1) | {"at": EPOCH + 5 * HOUR},
        _calendar() | {"at": EPOCH + 5 * HOUR},
    ]
    mixed = [triages[0], holds[0], triages[1], holds[1], holds[2], triages[2], holds[3]]
    for record in mixed:
        _validate(record)

    with_holds, without = _pipeline(), _pipeline()
    with_holds.rehydrate(mixed, now=NOW)
    without.rehydrate(triages, now=NOW)
    state = _state(with_holds)
    assert state == _state(without)
    assert "gatus:svc/example-d" not in state["cooldown"]
    assert state["cooldown"]["gatus:svc/example-a"] == EPOCH
    assert state["cap_times"] == [EPOCH, EPOCH + 2 * HOUR]


def test_a_v5_v6_mixed_log_rehydrates_like_its_v5_projection():
    # Version 6 changes no session field, so a log mixing v5 and v6 event triages
    # rehydrates exactly as the same log relabelled v5.
    v5 = _triage("gatus:svc/example-a", EPOCH, announceable=True, handoff="hf-a") | {
        "schema_version": 5
    }
    jsonschema.validate(v5, V5)
    v6 = _triage("gatus:svc/example-b", EPOCH + HOUR, announceable=True)
    log = [v5, v6]
    projection = [dict(r, schema_version=5) for r in log]
    for record in projection:
        jsonschema.validate(record, V5)
    a, b = _pipeline(), _pipeline()
    a.rehydrate(log, now=NOW)
    b.rehydrate(projection, now=NOW)
    assert _state(a) == _state(b)
    assert set(_state(a)["cooldown"]) == {"gatus:svc/example-a", "gatus:svc/example-b"}
