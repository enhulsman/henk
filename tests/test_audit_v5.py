"""Audit schema v5: the session record's profile, effort and evidence links (group 5).

From the audit-log spec's modified "Schema is versioned" (triage-quality change) and
design D16. Version 5 adds four optional, nullable fields to the session record —
`profile` (`chat` | `event`), `effort`, `recording_id` and `prior_handoff_ids` —
documents the session `outcome` values, and rewrites `memory_hash`'s description so a
non-null hash on an event-triage record is the documented case rather than a surprise.
No version-4 field is renamed, retyped or given a new meaning.

This group adds the document and the builder's keyword arguments only. Nothing here
populates the new fields from a live session: that is groups 7, 9 and 10. So the
defaults matter as much as the fields — an existing call site that passes none of them
must still produce a valid record.

Every fixture uses placeholder identities and ids; no live audit data.
"""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest

from henk.audit import (
    AUDIT_SCHEMA_PATH,
    AUDIT_SCHEMA_V1_PATH,
    AUDIT_SCHEMA_V2_PATH,
    AUDIT_SCHEMA_V3_PATH,
    AUDIT_SCHEMA_V4_PATH,
    AUDIT_SCHEMA_V5_PATH,
    SCHEMA_VERSION,
    reminder_record,
    session_record,
    suppression_record,
)
from henk.events.pipeline import EventPipeline, PipelineConfig
from henk.events.types import Event

SCHEMA = json.loads(AUDIT_SCHEMA_PATH.read_text())
V4 = json.loads(AUDIT_SCHEMA_V4_PATH.read_text())

#: The four fields v5 adds to the session record (design D16).
V5_FIELDS = ("profile", "effort", "recording_id", "prior_handoff_ids")


def _validate(record) -> None:
    jsonschema.validate(record, SCHEMA)


def _triage(**kw):
    """An event-triage session record with every v5 field populated."""
    base = dict(
        trigger="event",
        event=[{"identity_key": "gatus:svc/example-a", "source": "gatus"}],
        tool_calls=[{"name": "homelab_health", "tool_class": "read-only",
                     "executed": True}],
        handoff_message_id="hf-example-1",
        triage_arc_complete=True,
        announceable=True,
        profile="event",
        effort="high",
        recording_id="rec-example-1",
        prior_handoff_ids=[3, 17],
    )
    base.update(kw)
    return session_record(**base)


# --- New records declare the new version ---------------------------------


def test_the_current_version_is_five_and_its_document_is_committed():
    assert SCHEMA_VERSION == 5
    assert AUDIT_SCHEMA_V5_PATH == AUDIT_SCHEMA_PATH
    assert AUDIT_SCHEMA_PATH.name == "audit-record.v5.schema.json"
    assert SCHEMA["properties"]["schema_version"]["const"] == 5


def test_new_records_declare_the_new_version_and_validate_against_its_document():
    # Scenario "New records declare the new version": every builder, not only the
    # session record, stamps v5 and validates against the v5 document.
    for record in (
        session_record(trigger="owner-message"),
        _triage(),
        suppression_record(identity_key="gatus:svc/example-a", reason="cooldown"),
        reminder_record(reminder_id=1, due_at=1.0, transition="scheduled"),
    ):
        assert record["schema_version"] == 5
        _validate(record)


def test_a_v5_record_does_not_validate_as_v4():
    # Each document pins its own version, so a v5 record cannot pass as v4.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_triage(), V4)


# --- Old records remain valid (v1-v4) -------------------------------------


V1_RECORD = {
    "schema_version": 1, "record_type": "session", "trigger": "event",
    "outcome": "completed",
    "tool_calls": [{"name": "homelab_health", "tool_class": "read-only"}],
    "usage": {"input_tokens": 100, "output_tokens": 20}, "at": 1.0,
}
V2_RECORD = {
    "schema_version": 2, "record_type": "session", "trigger": "owner-message",
    "outcome": "completed", "tool_calls": [],
    "approvals": [{"tool": "spy", "decision": "approved"}], "at": 2.0,
}
V3_RECORD = {
    "schema_version": 3, "record_type": "session", "trigger": "event",
    "outcome": "completed", "tool_calls": [], "memory_hash": None, "at": 3.0,
}
V4_RECORD = {
    "schema_version": 4, "record_type": "session", "trigger": "event",
    "event": [{"identity_key": "gatus:svc/example-a"}],
    "outcome": "completed", "tool_calls": [], "handoff_message_id": "hf-example-1",
    "announceable": True, "memory_hash": None, "at": 4.0,
}


@pytest.mark.parametrize(
    "path, version, record",
    [
        (AUDIT_SCHEMA_V1_PATH, 1, V1_RECORD),
        (AUDIT_SCHEMA_V2_PATH, 2, V2_RECORD),
        (AUDIT_SCHEMA_V3_PATH, 3, V3_RECORD),
        (AUDIT_SCHEMA_V4_PATH, 4, V4_RECORD),
    ],
)
def test_old_records_remain_valid_against_their_own_document(path, version, record):
    # Scenario "Old records remain valid": every prior document stays committed,
    # pins its own version, and still accepts a record of that version.
    assert path.exists(), path
    document = json.loads(path.read_text())
    assert document["properties"]["schema_version"]["const"] == version
    jsonschema.validate(record, document)
    # ...and v5's document is not a stand-in for it.
    with pytest.raises(jsonschema.ValidationError):
        _validate(record)


def test_the_v4_document_is_the_one_that_shipped_not_a_rewrite():
    # v1-v4 "stay" (D16): v4 still declares the reminder record type and does not
    # know the v5 session fields, so a v4 reader was never told about them.
    assert AUDIT_SCHEMA_V4_PATH.name == "audit-record.v4.schema.json"
    assert AUDIT_SCHEMA_V4_PATH != AUDIT_SCHEMA_PATH
    assert "reminder" in V4["properties"]["record_type"]["enum"]
    for field in V5_FIELDS:
        assert field not in V4["properties"], field


# --- Every v5 field is optional and nullable ------------------------------


@pytest.mark.parametrize("field", V5_FIELDS)
def test_every_v5_field_is_optional(field):
    record = _triage()
    del record[field]
    _validate(record)


@pytest.mark.parametrize("field", V5_FIELDS)
def test_every_v5_field_is_nullable(field):
    _validate(_triage(**{field: None}))


def test_a_session_record_with_none_of_the_v5_fields_validates():
    # The v4-shaped call site: absent on the wire as well as null.
    record = session_record(trigger="owner-message")
    for field in V5_FIELDS:
        record.pop(field, None)
    _validate(record)


def test_the_v5_fields_are_not_required_on_any_record_type():
    required = set(SCHEMA.get("required", []))
    for branch in SCHEMA.get("allOf", []):
        required |= set(branch.get("then", {}).get("required", []))
    assert not required & set(V5_FIELDS)


def test_the_builder_defaults_every_v5_field_to_null():
    # Existing call sites pass none of the new keyword arguments (core.py's
    # session_record call). They must keep producing a valid record, with the new
    # keys present and null — the same always-emit convention as memory_hash.
    record = session_record(trigger="owner-message")
    for field in V5_FIELDS:
        assert field in record, field
        assert record[field] is None, field
    _validate(record)


def test_the_builder_carries_every_v5_keyword_into_the_record():
    record = _triage()
    assert record["profile"] == "event"
    assert record["effort"] == "high"
    assert record["recording_id"] == "rec-example-1"
    assert record["prior_handoff_ids"] == [3, 17]
    _validate(record)


def test_prior_handoff_ids_is_copied_not_aliased():
    ids = [3]
    record = _triage(prior_handoff_ids=ids)
    ids.append(99)
    assert record["prior_handoff_ids"] == [3]


def test_an_empty_prior_handoff_list_is_kept_distinct_from_null():
    # The spec: an event triage that showed no prior handoffs carries an EMPTY list;
    # a non-triage record carries null. The builder must not collapse the two.
    assert _triage(prior_handoff_ids=[])["prior_handoff_ids"] == []
    assert _triage(prior_handoff_ids=None)["prior_handoff_ids"] is None
    _validate(_triage(prior_handoff_ids=[]))


def test_both_profiles_validate():
    for profile in ("chat", "event"):
        _validate(_triage(profile=profile))


@pytest.mark.parametrize(
    "field, bad",
    [
        ("profile", "triage"),
        ("profile", "Event"),
        ("profile", 1),
        ("effort", 3),
        ("effort", ["high"]),
        ("recording_id", 7),
        ("recording_id", {"id": "rec"}),
        ("prior_handoff_ids", "hf-example-0"),
        ("prior_handoff_ids", ["hf-example-0"]),
        ("prior_handoff_ids", ["3"]),
        ("prior_handoff_ids", [1.5]),
        ("prior_handoff_ids", [True]),
        ("prior_handoff_ids", [None]),
    ],
)
def test_the_v5_fields_are_typed(field, bad):
    # Optional and nullable is not "anything goes": a present value has a type. Set on
    # the record directly, so the DOCUMENT does the refusing, not the builder — a
    # hand-built record gets no help from session_record.
    record = _triage()
    record[field] = bad
    with pytest.raises(jsonschema.ValidationError):
        _validate(record)


def test_the_builder_refuses_a_bare_string_for_prior_handoff_ids():
    # list("hf-1") is ["h", "f", "-", "1"]: a valid-looking list of false references.
    # A link that points at nothing is worse than no link, so the builder refuses.
    with pytest.raises(TypeError):
        _triage(prior_handoff_ids="hf-example-0")


@pytest.mark.parametrize("bad", [["3"], [True], [1.0]])
def test_the_builder_refuses_non_integer_handoff_ids(bad):
    # A row id is an int. A stringified or boolean id would validate nowhere, and
    # the audit write must not be the first place that notices.
    with pytest.raises(TypeError):
        _triage(prior_handoff_ids=bad)


def test_the_evidence_links_are_references_not_content():
    # "These fields are references, not content": a recording id is a string, and
    # the prior handoffs are the handoff archive's integer row ids (D8's `id`, which
    # is never null, unlike its `message_id`). No object slot exists through which a
    # recording, a handoff document or a tool result could be embedded.
    props = SCHEMA["properties"]
    assert set(props["recording_id"]["type"]) == {"string", "null"}
    assert set(props["prior_handoff_ids"]["type"]) == {"array", "null"}
    assert props["prior_handoff_ids"]["items"] == {"type": "integer"}
    assert "row ids" in props["prior_handoff_ids"]["description"]
    assert "message id" not in props["prior_handoff_ids"]["description"]


# --- memory_hash on event records; documented outcomes --------------------


def test_an_event_record_with_a_non_null_memory_hash_validates():
    record = _triage(memory_hash="sha256:" + "0" * 64)
    assert record["trigger"] == "event"
    assert record["memory_hash"] is not None
    _validate(record)


def test_memory_hash_description_no_longer_says_event_sessions_are_null():
    # v4's text: "Null when no block was injected (empty store, or a session that
    # only ran event turns)." Event turns now receive recall (D7), so v5 rewrites it.
    v4_text = V4["properties"]["memory_hash"]["description"]
    v5_text = SCHEMA["properties"]["memory_hash"]["description"]
    assert "only ran event turns" in v4_text
    assert "only ran event turns" not in v5_text
    assert "event" in v5_text
    # A rewritten description, not a retyped field.
    assert SCHEMA["properties"]["memory_hash"]["type"] == V4["properties"][
        "memory_hash"
    ]["type"]


def test_the_session_outcome_values_are_documented():
    description = SCHEMA["properties"]["outcome"]["description"]
    for value in ("completed", "error", "refused", "no-reply"):
        assert f"`{value}`" in description, value


@pytest.mark.parametrize("outcome", ["completed", "error", "refused", "no-reply"])
def test_every_documented_outcome_validates(outcome):
    _validate(_triage(outcome=outcome))


def test_no_v4_field_is_changed():
    # "Version 5 SHALL NOT rename, retype, or change the meaning of any version-4
    # field." Descriptions aside (memory_hash and outcome are re-documented), every
    # v4 property and every v4 conditional branch is carried over unchanged.
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "description"}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    v4_props = strip(V4["properties"])
    v5_props = strip(SCHEMA["properties"])
    for name, spec in v4_props.items():
        if name == "schema_version":
            continue
        assert name in v5_props, name
        assert v5_props[name] == spec, name
    assert set(v5_props) - set(v4_props) == set(V5_FIELDS)
    assert strip(SCHEMA["allOf"]) == strip(V4["allOf"])
    assert SCHEMA.get("required") == V4.get("required")
    assert SCHEMA.get("additionalProperties") == V4.get("additionalProperties")


# --- A mixed-version log rehydrates identically ---------------------------


HOUR = 3600.0
EPOCH = 1_700_000_000.0
NOW = EPOCH + 8.5 * HOUR  # off every window boundary


def _pipeline() -> EventPipeline:
    return EventPipeline(
        PipelineConfig(
            debounce_seconds=120.0,
            cooldown_seconds=6 * HOUR,
            recurrence_window_seconds=24 * HOUR,
            cap_per_24h=3,
            cooldown_overrides=[
                {"pattern": "example-b", "cooldown_seconds": 12 * HOUR}
            ],
        )
    )


def _state(pipe: EventPipeline) -> dict:
    """The cadence state rehydration reconstructs (events/pipeline.py:64-68)."""
    return {
        "cooldown": dict(pipe._last_triaged),
        "recurrence": dict(pipe._last_handoff_ref),
        "cap_times": list(pipe._announce_times),
        "cap_suppressed": pipe._cap_suppressed_since_announce,
    }


def _v4(identity: str, at: float, *, announceable, handoff=None) -> dict:
    record = {
        "schema_version": 4, "record_type": "session", "trigger": "event",
        "event": [{"identity_key": identity}], "outcome": "completed",
        "tool_calls": [], "handoff_message_id": handoff,
        "announceable": announceable, "memory_hash": None, "at": at,
    }
    jsonschema.validate(record, V4)
    return record


def _v5(identity: str, at: float, *, announceable, handoff=None, **v5) -> dict:
    record = session_record(
        trigger="event",
        event=[{"identity_key": identity}],
        handoff_message_id=handoff,
        announceable=announceable,
        memory_hash="sha256:" + "1" * 64,
        profile="event",
        effort="max",
        recording_id=v5.get("recording_id", f"rec-{identity}"),
        prior_handoff_ids=v5.get("prior_handoff_ids", [1]),
        at=at,
    )
    _validate(record)
    return record


def _mixed_log() -> list[dict]:
    # Interleaved versions, exercising all three pieces of cadence state: cooldown
    # (per identity, incl. an override), recurrence refs (a later v5 handoff must
    # win over an earlier v4 one for the same identity), and the cap window
    # (announced and cap-suppressed triages across both versions).
    return [
        _v4("gatus:svc/example-a", EPOCH, announceable=True, handoff="hf-a-v4"),
        _v5("gatus:svc/example-b", EPOCH + 60, announceable=True,
            handoff="hf-b-v5", prior_handoff_ids=[]),
        _v4("gatus:svc/example-c", EPOCH + 120, announceable=False),
        _v5("gatus:svc/example-a", EPOCH + 2 * HOUR, announceable=False,
            handoff="hf-a-v5", recording_id=None),
        suppression_record(identity_key="gatus:svc/example-a", reason="cooldown")
        | {"at": EPOCH + 3 * HOUR},
        _v5("gatus:svc/example-d", EPOCH + 4 * HOUR, announceable=True,
            handoff=None, prior_handoff_ids=None),
        _v4("gatus:svc/example-e", EPOCH + 5 * HOUR, announceable=False),
        session_record(trigger="owner-message", profile="event", effort="max",
                       at=EPOCH + 5 * HOUR + 60),
    ]


def _v4_projection(log: list[dict]) -> list[dict]:
    """The same log with every v5-only field removed, declaring v4."""
    projected = []
    for record in copy.deepcopy(log):
        if record.get("schema_version") == 5:
            for field in V5_FIELDS:
                record.pop(field, None)
            record["schema_version"] = 4
            jsonschema.validate(record, V4)
        projected.append(record)
    return projected


def test_a_mixed_version_log_rehydrates_identically():
    log = _mixed_log()
    assert {r["schema_version"] for r in log} == {4, 5}
    # The projection really removed something, or the comparison proves nothing.
    assert any(f in r for r in log for f in V5_FIELDS)
    projection = _v4_projection(log)
    assert not any(f in r for r in projection for f in V5_FIELDS)

    mixed, projected = _pipeline(), _pipeline()
    mixed.rehydrate(log, now=NOW)
    projected.rehydrate(projection, now=NOW)

    state = _state(mixed)
    assert state == _state(projected)
    # And the state is the non-trivial one the log describes, not two empty states.
    assert state["cooldown"] == {
        "gatus:svc/example-a": EPOCH + 2 * HOUR,
        "gatus:svc/example-b": EPOCH + 60,
        "gatus:svc/example-c": EPOCH + 120,
        "gatus:svc/example-d": EPOCH + 4 * HOUR,
        "gatus:svc/example-e": EPOCH + 5 * HOUR,
    }
    assert state["recurrence"] == {
        "gatus:svc/example-a": "hf-a-v5",
        "gatus:svc/example-b": "hf-b-v5",
    }
    assert state["cap_times"] == [EPOCH, EPOCH + 60, EPOCH + 4 * HOUR]
    assert state["cap_suppressed"] == 1


def test_a_mixed_version_log_yields_the_same_decisions_after_restart():
    # The same claim, observed through behaviour rather than private state.
    log = _mixed_log()
    decisions = []
    for records in (log, _v4_projection(log)):
        pipe = _pipeline()
        pipe.rehydrate(records, now=NOW)
        batch = [
            Event(id=f"e-{name}", title=f"Gatus: svc/{name}", message="",
                  arrival_time=NOW)
            for name in ("example-a", "example-b", "example-f")
        ]
        decision = pipe.evaluate(batch, now=NOW)
        turn = decision.event_turn
        decisions.append((
            sorted((s.identity_key, s.reason) for s in decision.suppressions),
            None if turn is None else (
                turn.announceable,
                turn.suppressed_count,
                sorted((i.identity.key, i.recurrence, i.prior_handoff_ref)
                       for i in turn.items),
            ),
        ))
    assert decisions[0] == decisions[1]
